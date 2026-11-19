"""In-memory controller and local HTTP fixture. Never imports hardware code."""
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import RLock, Thread


class FakeMachine:
    def __init__(self):
        self.calls = []
        self.lock = RLock()
        self.offline = False
        self.state = {
            'connection': {'connected': True, 'port': 'SIMULATED', 'firmware': 'Test fixture'},
            'mode': 'NORMAL', 'stats': {'uptime_ms': 5000}, 'logs': [],
            'lease': {'active': False, 'client_id': None, 'operator_name': None},
            'triggers': {'enabled': False, 'frequency_hz': 59.5, 'low_us': 50},
            'strobe': {'enabled': False, 'lead_us': 250, 'lag_us': 10},
            'high_speed': {'enabled': False, 'available': True, 'valid': True, 'mode': 'multiple',
                'multiplier': 3, 'fixed_frequency_hz': 180, 'pulse_us': 50, 'illumination': True, 'frequency_hz': 178.5},
            'vibration_motor': {'enabled': False, 'frequency_hz': 4000, 'duty_percent': 38, 'direction': 'clockwise'},
            'conveyor': {'enabled': False, 'speed_sps': 20, 'acceleration_sps2': 10, 'direction': 'reverse',
                'driver_enabled': False, 'current_speed_sps': 0, 'min_speed_sps': 10, 'max_speed_sps': 5000},
            'valves': {'available': True, 'connected': True, 'synchronized': True, 'testing': False, 'busy': False},
        }

    def request(self, connection, action, body=None):
        with self.lock:
            self.calls.append((action, deepcopy(body)))
            if self.offline: return 503, {'detail': 'Fixture disconnected'}
            if action == 'state': return 200, deepcopy(self.state)
            body = body or {}
            lease = self.state['lease']
            if action == 'control/acquire':
                if lease['active'] and lease['client_id'] != body['client_id']:
                    return 409, {'detail': 'Another operator currently has control.'}
                lease.update(active=True, client_id=body['client_id'], operator_name=body['operator_name'])
                return 200, deepcopy(self.state)
            if action not in {'stop', 'valves/stop'} and (not lease['active'] or lease['client_id'] != body['client_id']):
                return 409, {'detail': 'This browser does not hold the control lease.'}
            if action == 'control/heartbeat': return 200, deepcopy(lease)
            if action in {'control/release', 'stop'}:
                for key in ('triggers', 'strobe', 'high_speed', 'vibration_motor', 'conveyor'):
                    self.state[key]['enabled'] = False
                self.state['conveyor'].update(driver_enabled=False, current_speed_sps=0)
                if action == 'control/release': lease.update(active=False, client_id=None, operator_name=None)
                return 200, {'ok': True}
            if action == 'valves/test': return 200, {'ok': True, 'valves': deepcopy(self.state['valves'])}
            if '/' in action:
                group, operation = action.split('/')
                group = {'vibration': 'vibration_motor', 'high-speed': 'high_speed'}.get(group, group)
                if operation == 'config':
                    if group == 'vibration_motor' and not 0 <= body['duty_percent'] <= 100:
                        return 422, {'detail': 'Duty must be from 0.0% to 100.0%.'}
                    self.state[group].update({k:v for k,v in body.items() if k != 'client_id'})
                elif operation == 'enabled': self.state[group]['enabled'] = body['enabled']
                if group == 'conveyor':
                    self.state[group].update(driver_enabled=self.state[group]['enabled'],
                        current_speed_sps=self.state[group]['speed_sps'] if self.state[group]['enabled'] else 0)
            return 200, {'ok': True}


@contextmanager
def machine_server(machine):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply(None)

        def do_POST(self):
            self.reply(json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}'))

        def reply(self, body):
            if not self.path.startswith('/api/'):
                self.send_error(404); return
            status, payload = machine.request(None, self.path[len('/api/'):], body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True);thread.start()
    try: yield server.server_port
    finally: server.shutdown();server.server_close();thread.join(timeout=3)
