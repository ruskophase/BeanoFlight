"""Manual HTTP controls; the existing machine service retains hardware authority."""
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request as RemoteRequest, build_opener

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .auth import require_role
from .models import Audit, identity
from .quantities import DomainError

CONFIG_ACTION = 'sorter_controls_connection'
POST_ACTIONS = frozenset({
    'control/acquire', 'control/heartbeat', 'control/release', 'stop',
    'triggers/config', 'triggers/enabled', 'triggers/pulse',
    'strobe/config', 'strobe/enabled', 'strobe/pulse',
    'high-speed/config', 'high-speed/enabled',
    'vibration/config', 'vibration/enabled', 'conveyor/config', 'conveyor/enabled',
    'valves/test', 'valves/stop',
})


@dataclass(frozen=True)
class Connection:
    id: str
    host: str
    port: int

    @property
    def url(self):
        host = '[' + self.host + ']' if ':' in self.host else self.host
        return f'http://{host}:{self.port}'


def validate_address(host, port):
    host = str(host).strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise DomainError('Enter only an IP address or hostname, without a URL or port') from None
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
            raise DomainError('Enter a valid machine hostname')
        host = host.lower()
    else:
        if address.is_unspecified or address.is_multicast:
            raise DomainError('Enter the machine IP address or hostname')
        host = str(address)
    if isinstance(port, bool) or not str(port).isascii() or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise DomainError('Enter a port from 1 to 65535')
    return host, int(port)


def connection_for(session, settings):
    record = session.scalar(select(Audit).where(Audit.action == CONFIG_ACTION)
        .order_by(Audit.created_at.desc(), Audit.id.desc()).limit(1))
    if record:
        return Connection(record.id, record.data['host'], record.data['port'])
    host, port = validate_address(settings.sorter_host, settings.sorter_port)
    return Connection('default', host, port)


class RemoteUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MachineHTTP:
    def request(self, connection, action, body=None):
        request = RemoteRequest(connection.url + '/api/' + action,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
        # No redirects, environment proxies, serial connections, retries or background keepalives.
        opener = build_opener(ProxyHandler({}), NoRedirect())
        try:
            try:
                response = opener.open(request, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise RemoteUnavailable('The machine response was too large')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object')
                return response.status, payload
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise RemoteUnavailable('Cannot confirm a response from the machine. Check its address and connection; commands are not retried.') from error


def register_routes(app, db, settings, user_for, render, form):
    app.state.sorter_transport = MachineHTTP()

    def pinned(request):
        with db.read() as session:
            connection = connection_for(session, settings)
        if request.headers.get('X-Sorter-Connection') != connection.id:
            raise HTTPException(409, 'Machine address changed. Reload Sorter Controls before continuing.')
        return connection

    def client_ids(request, user, connection):
        browser_id = request.headers.get('X-Sorter-Client', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', browser_id):
            raise HTTPException(400, 'Invalid control client')
        identity_text = '\0'.join([user.id, request.session['csrf'], connection.id, browser_id])
        machine_id = 'beanosort-' + hmac.new(settings.secret.encode(), identity_text.encode(), hashlib.sha256).hexdigest()
        return browser_id, machine_id

    def mapped(payload, browser_id, machine_id):
        if isinstance(payload.get('lease'), dict) and payload['lease'].get('client_id') == machine_id:
            payload = {**payload, 'lease': {**payload['lease'], 'client_id': browser_id}}
        return payload

    def forward(connection, action, body=None):
        try:
            return app.state.sorter_transport.request(connection, action, body)
        except RemoteUnavailable as error:
            return 503, {'detail': str(error)}

    @app.get('/sorter-controls')
    def controls_page(request: Request):
        user = user_for(request)
        with db.read() as session:
            connection = connection_for(session, settings)
        return render(request, 'sorter_controls.html', user=user, connection=connection)

    @app.post('/sorter-controls/connection')
    async def save_connection(request: Request):
        user = user_for(request)
        require_role(user, 'admin', 'supervisor')
        data = await form(request)
        host, port = validate_address(data.get('host', ''), data.get('port', ''))
        with db.read() as session:
            previous = connection_for(session, settings)
        if data.get('connection_id') != previous.id:
            raise DomainError('Machine address changed in another session. Reload before saving.')
        if (host, port) != (previous.host, previous.port) or previous.id == 'default':
            status, state = await run_in_threadpool(forward, previous, 'state')
            if status == 200 and state.get('lease', {}).get('active'):
                raise DomainError('Release the machine control lease before changing its address')
        with db.write() as session:
            if connection_for(session, settings).id != previous.id:
                raise DomainError('Machine address changed in another session. Reload before saving.')
            if (host, port) != (previous.host, previous.port) or previous.id == 'default':
                session.add(Audit(id=identity('audit'), operator_id=user.id, action=CONFIG_ACTION,
                    data={'host': host, 'port': port, 'previous_connection_id': previous.id}))
        from fastapi.responses import RedirectResponse
        return RedirectResponse('/sorter-controls', status_code=303)

    @app.get('/sorter-controls/panel')
    def controls_panel(request: Request):
        user = user_for(request)
        with db.read() as session:
            connection = connection_for(session, settings)
        response = render(request, 'sorter_controls_panel.html', user=user, connection=connection)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
        return response

    @app.get('/sorter-controls/api/state')
    def state(request: Request):
        user = user_for(request)
        connection = pinned(request)
        browser_id, machine_id = client_ids(request, user, connection)
        status, payload = forward(connection, 'state')
        return JSONResponse(mapped(payload, browser_id, machine_id), status_code=status, headers={'Cache-Control': 'no-store'})

    @app.post('/sorter-controls/api/{action:path}')
    async def command(request: Request, action: str):
        user = user_for(request)
        require_role(user, 'operator', 'supervisor', 'admin')
        expected = request.session.get('csrf')
        if not expected or not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), expected):
            raise HTTPException(403, 'Invalid CSRF token; reload Sorter Controls')
        if action not in POST_ACTIONS:
            raise HTTPException(404, 'Unknown machine control')
        connection = pinned(request)
        browser_id, machine_id = client_ids(request, user, connection)
        raw = await request.body()
        if len(raw) > 16_384:
            raise HTTPException(413, 'Control request too large')
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict): raise ValueError()
            json.dumps(body, allow_nan=False)
        except (ValueError, TypeError):
            raise HTTPException(422, 'Enter valid control settings') from None
        body['client_id'] = machine_id
        if action == 'control/acquire':
            body['operator_name'] = user.username[:64]

        def perform():
            status, payload = forward(connection, action, body)
            # Heartbeats must not compete with inventory transactions or inflate the audit log.
            if action != 'control/heartbeat':
                with db.write() as session:
                    session.add(Audit(id=identity('audit'), operator_id=user.id, action='sorter_control_command',
                        data={'connection_id': connection.id, 'host': connection.host, 'port': connection.port,
                            'command': action, 'parameters': {k:v for k,v in body.items() if k != 'client_id'},
                            'http_status': status, 'acknowledged': 200 <= status < 300}))
            return JSONResponse(mapped(payload, browser_id, machine_id), status_code=status, headers={'Cache-Control': 'no-store'})
        return await run_in_threadpool(perform)
