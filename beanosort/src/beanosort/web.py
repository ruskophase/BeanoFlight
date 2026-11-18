from datetime import datetime, timezone
from contextlib import asynccontextmanager
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import secrets
from itsdangerous import BadSignature, URLSafeTimedSerializer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import qrcode
import qrcode.image.svg
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .auth import require_role, verify_password
from .capture import CAPTURE_DEFAULT, expected_profile, fixture_particle
from .config import Settings
from .db import Database
from .models import (Assignment, Audit, Container, Destination, HandlingPlan, Input, Ledger, Measurement, Operation, Output,
                     Location, Pack, Particle, Portion, ProductLot, Profile, Receipt, Reconciliation, Reservation, Run,
                     ReferenceDataset, SimulationAssumption, SimulationSettings, Station, User, Version, now)
from .quantities import DomainError, kg, kg3, mass
from .service import (Workflow, active_assignment, audit, available, balance, container_usage, deleted_container, deleted_location,
                      get, prepared_snapshot, require_unstarted_sorting, trace)
from .handling import MaterialHandling, effective_outputs, effective_reconciliation
from .simulation import (MAX_SIMULATION_PARTICLES, RESEARCH, RESEARCH_NOTE, SimulationConfiguration,
                         current_settings, estimate_particles, mean_mass_g)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    db = Database(settings.database_url, settings.environment)
    workflow = Workflow(db)
    from .machine_jobs import Integration
    machine = Integration(db, workflow, settings)
    handling = MaterialHandling(db)
    from .material import MaterialActions
    material_actions = MaterialActions(db)
    simulation_config = SimulationConfiguration(db)
    weighing_signer = URLSafeTimedSerializer(settings.secret, salt="beanosort-container-weighing-review/v1")
    material_signer = URLSafeTimedSerializer(settings.secret, salt="beanosort-material-state/v1")
    root = Path(__file__).parent
    @asynccontextmanager
    async def lifespan(app):
        machine.launch()
        try: yield
        finally: machine.close()
    app = FastAPI(title="BeanoSort", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.db, app.state.workflow, app.state.handling = db, workflow, handling
    app.state.machine_jobs = machine
    app.add_middleware(SessionMiddleware, secret_key=settings.secret, session_cookie="beanosort_session",
                       same_site="lax", https_only=settings.secure_cookies, max_age=8 * 60 * 60)
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    templates = Jinja2Templates(directory=root / "templates")
    templates.env.filters["kg"] = kg
    templates.env.filters["kg3"] = kg3
    templates.env.filters["integer"] = lambda value: format(int(value), ",")
    templates.env.filters["localtime"] = lambda value: datetime.fromisoformat(value).astimezone(ZoneInfo(settings.timezone)).strftime("%d %b %Y %H:%M:%S %Z") if value else "No data received"
    templates.env.globals["newkey"] = lambda: secrets.token_urlsafe(24)

    def user_for(request):
        with db.read() as s:
            user = s.get(User, request.session.get("user_id", ""))
            if user is None or not user.active:
                raise HTTPException(401)
            return user

    def render(request, name, *, user=None, status=200, db_session=None, **context):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        descriptions = {}
        operation_descriptions = {}
        container_labeler = None
        def container_description(container):
            nonlocal container_labeler
            if container_labeler is None:
                if db_session is None:
                    raise RuntimeError("Container descriptions require the page's read session")
                from .container_labels import option_labeler
                container_labeler = option_labeler(db_session)
            return container_labeler(container)
        def portion_description(portion_id):
            from .portion_labels import describe_portion
            if portion_id not in descriptions:
                if db_session is None:
                    raise RuntimeError("Material descriptions require the page's read session")
                describe_portion(db_session, portion_id, descriptions)
            return descriptions.get(portion_id, "Creation history unavailable")
        def operation_description(operation_id):
            from .operation_labels import describe_operation
            if operation_id not in operation_descriptions:
                if db_session is None:
                    raise RuntimeError("Operation descriptions require the page's read session")
                operation_descriptions[operation_id] = describe_operation(db_session, operation_id)
            return operation_descriptions[operation_id]
        def profile_charts_url(profile):
            if db_session is None:return None
            from .measurement_charts import profile_url
            return profile_url(db_session,profile)
        return templates.TemplateResponse(request=request, name=name, context={
            "request": request, "user": user, "environment": settings.environment,
            "csrf": request.session["csrf"], "timezone": settings.timezone,
            "stale_seconds": settings.station_stale_seconds, "locations_enabled": True,
            "container_move_enabled": True, "location_delete_enabled": True, "recipes_enabled": True,
            "container_weighing_enabled": True, "portion_description": portion_description,
            "operation_description": operation_description,
            "container_description": container_description,
            "profile_charts_url": profile_charts_url,"measurement_charts_enabled": True,
            "segment_ui_enabled": True,
            **context}, status_code=status)

    def age_seconds(timestamp):
        return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds() if timestamp else None

    async def form(request):
        raw = await request.form()
        data = dict(raw)
        if "portion_ids" in raw:
            data["portion_ids"] = raw.getlist("portion_ids")
        if "receiving_load_id" in raw:
            for field in ("receiving_load_id", "receiving_container_id", "receiving_net_mass",
                          "receiving_location_id", "receiving_container_revision", "tared",
                          "receiving_tare_mass", "receiving_capacity"):
                data[field] = raw.getlist(field)
        expected = request.session.get("csrf")
        if not expected or not secrets.compare_digest(str(data.get("csrf", "")), expected):
            raise HTTPException(403, "Invalid CSRF token; reload the form")
        return data

    def redirect(path):
        return RedirectResponse(path, status_code=303)

    def evidence(data):
        return {"kind": data.get("evidence_kind"), "method": data.get("method", ""),
                "device": data.get("device") or None, "tare_basis": data.get("tare_basis", ""),
                "precision_mg": data.get("precision_mg") or None}

    def portion_view(s, portion):
        assignment = active_assignment(s, portion_id=portion.id)
        container = s.get(Container, assignment.container_id) if assignment else None
        receipt = s.get(Receipt, portion.receipt_id) if portion.receipt_id else None
        assumption = s.scalar(select(SimulationAssumption).filter_by(operation_id=receipt.operation_id)) if receipt else None
        return {"portion": portion, "mass": balance(s, portion.id), "available": available(s, portion),
                "container": container, "receipt": receipt, "count_estimate": assumption.data if assumption else None}

    def simulation_view(s, item):
        if item is None:
            return None
        return {"record": item, "mean_mass_g": mean_mass_g(item),
                "capture_percent": format(Decimal(get(s, Version, item.capture_model_id).data["probability"]) * 100, "f"),
                "per_kg": estimate_particles(1_000_000, item)["rounded_particles"],
                "per_20kg": estimate_particles(20_000_000, item)["rounded_particles"]}

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 401:
            return redirect("/login")
        return render(request, "error.html", status=exc.status_code, message=str(exc.detail))

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return render(request, "error.html", status=409, message=str(exc))

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return render(request, "error.html", status=409,
                      message="This conflicts with an existing identity, reservation or immutable record. Reload and inspect the job before retrying.")

    @app.get("/health")
    def health():
        with db.read():
            return {"status": "ok", "environment": settings.environment, "hardware_control": True, "automatic_job_control": machine.enabled}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request):
        data = await form(request)
        with db.read() as s:
            user = s.scalar(select(User).filter_by(username=data.get("username"), active=True))
            if user is None or not await run_in_threadpool(verify_password, data.get("password", ""), user.password_hash):
                return render(request, "login.html", db_session=s, status=401, error="Username or password not accepted")
            request.session.clear()
            request.session.update(user_id=user.id, csrf=secrets.token_urlsafe(32))
        return redirect("/")

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        request.session.clear()
        return redirect("/login")

    @app.get("/", response_class=HTMLResponse)
    def stock(request: Request):
        user = user_for(request)
        with db.read() as s:
            stock = [portion_view(s, p) for p in s.scalars(select(Portion).where(Portion.status != "consumed").order_by(Portion.created_at.desc()))]
            removed_jobs = select(Audit.operation_id).where(Audit.action.in_(["job_deleted", "job_replaced"]), Audit.operation_id.is_not(None))
            jobs = list(s.scalars(select(Operation).where(Operation.kind == "sorting", ~Operation.id.in_(removed_jobs)).order_by(Operation.created_at.desc())))
            from .collection_views import rows as collection_view_rows
            pending_collections=[{"op":op,**row} for op in jobs if op.status not in {"completed","cancelled"} for row in collection_view_rows(s,op)]
            stations = []
            for station in s.scalars(select(Station)):
                age = age_seconds(station.last_received_at)
                stations.append({"station": station, "age": age, "fresh": age is not None and age < settings.station_stale_seconds})
            return render(request, "stock.html", db_session=s, user=user, stock=stock, jobs=jobs, stations=stations,pending_collections=pending_collections,
                          available_mg=sum(x["available"] for x in stock), held_mg=sum(x["mass"] for x in stock if x["portion"].quality == "held"))

    @app.get("/locations", response_class=HTMLResponse)
    def locations_page(request: Request):
        user = user_for(request)
        with db.read() as s:
            rows = s.execute(select(Location, func.count(Container.id)).outerjoin(Container, Container.location_id == Location.id)
                .group_by(Location.id, Location.name, Location.name_key, Location.created_at, Location.revision).order_by(Location.name)).all()
            return render(request, "locations.html", db_session=s, user=user, location_rows=rows)

    @app.post("/locations")
    async def create_location(request: Request):
        user = user_for(request)
        data = await form(request)
        key = await run_in_threadpool(workflow.create_location, user.id, data["key"], data.get("name"))
        return redirect("/locations/" + key)

    @app.get("/locations/{key}", response_class=HTMLResponse)
    def location_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            location = s.get(Location, key)
            if location is None:
                deletion = deleted_location(s, key)
                if deletion:
                    return render(request, "location_deleted.html", db_session=s, user=user, deletion=deletion, status=410)
                raise DomainError("Unknown location identifier")
            from .collection_views import for_container
            rows = []
            for container in s.scalars(select(Container).filter_by(location_id=key, deleted=False).order_by(Container.label)):
                assignment = active_assignment(s, container_id=container.id)
                contents = portion_view(s, get(s, Portion, assignment.portion_id)) if assignment else {
                    "portion": None, "mass": 0, "available": 0, "receipt": None, "count_estimate": None}
                rows.append({**contents, "container": container, "assignment": assignment,
                             "work_collection": for_container(s, container.id)})
            changes = list(s.scalars(select(Audit).where(Audit.data["location_id"].as_string() == key).order_by(Audit.created_at.desc())))
            return render(request, "location.html", db_session=s, user=user, location=location, containers=rows, changes=changes)

    @app.get("/locations/{key}/edit", response_class=HTMLResponse)
    def edit_location_page(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            return render(request, "location_edit.html", db_session=s, user=user, location=get(s, Location, key))

    @app.get("/locations/{key}/delete", response_class=HTMLResponse)
    def location_delete_page(request: Request, key: str):
        user = user_for(request)
        require_role(user, "supervisor", "admin")
        with db.read() as s:
            location = get(s, Location, key)
            containers = list(s.scalars(select(Container).filter_by(location_id=key).order_by(Container.label)))
            return render(request, "location_delete.html", db_session=s, user=user, location=location, containers=containers)

    @app.post("/locations/{key}/{action}")
    async def change_location(request: Request, key: str, action: str):
        user = user_for(request)
        data = await form(request)
        if action == "edit":
            await run_in_threadpool(workflow.update_location, user.id, data["key"], key, name=data.get("name"), revision=data.get("revision"))
        elif action == "delete":
            await run_in_threadpool(workflow.delete_location, user.id, data["key"], key,
                                   revision=data.get("revision"), confirmed=data.get("confirmed") == "on")
            return redirect("/locations")
        else:
            raise HTTPException(404, "Unknown location action")
        return redirect("/locations/" + key)

    @app.get("/containers", response_class=HTMLResponse)
    def containers(request: Request, include_archived: bool = False):
        user = user_for(request)
        with db.read() as s:
            query = select(Container).where(Container.deleted == False).order_by(Container.label)
            if not include_archived:
                query = query.where(Container.archived == False, Container.deleted == False)
            rows = []
            for container in s.scalars(query):
                from .collection_views import for_container
                assignment = active_assignment(s, container_id=container.id)
                rows.append({"container": container, "assignment": assignment,
                             "work_collection":for_container(s,container.id),
                             "mass": balance(s, assignment.portion_id) if assignment else 0})
            return render(request, "containers.html", db_session=s, user=user, containers=rows, include_archived=include_archived,
                          locations=list(s.scalars(select(Location).order_by(Location.name))))

    @app.post("/containers")
    async def container_create(request: Request):
        user = user_for(request)
        data = await form(request)
        await run_in_threadpool(workflow.create_container, user.id, label=data["label"], location=data.get("location"), location_id=data.get("location_id"),
                                kind=data["kind"], capacity=data.get("capacity"), tare_mass=data.get("tare_mass"))
        return redirect("/containers")

    @app.get("/containers/{key}", response_class=HTMLResponse)
    def container_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            container = s.get(Container, key)
            if container is None or container.deleted:
                deletion = deleted_container(s, key)
                if deletion:
                    contents_history = list(s.scalars(select(Assignment).filter_by(container_id=key).order_by(Assignment.created_at.desc())))
                    return render(request, "container_deleted.html", db_session=s, user=user, deletion=deletion, contents_history=contents_history, status=410)
                raise DomainError("Unknown container identifier")
            assignment = active_assignment(s, container_id=key)
            current = portion_view(s, get(s, Portion, assignment.portion_id)) if assignment else None
            history = list(s.scalars(select(Assignment).filter_by(container_id=key).order_by(Assignment.created_at.desc())))
            claim = s.scalar(select(Destination).filter_by(container_id=key, active=True))
            profiles = list(s.scalars(select(Profile).filter_by(portion_id=assignment.portion_id).order_by(Profile.created_at.desc(), Profile.version.desc()))) if assignment else []
            changes = list(s.scalars(select(Audit).where(Audit.data["container_id"].as_string() == key).order_by(Audit.created_at.desc())))
            registration = next((row for row in changes if row.data.get("tare_evidence")), None)
            usage = container_usage(s, container)
            from .collection_views import for_container
            return render(request, "container.html", db_session=s, user=user, container=container, current=current, history=history,
                          claim=claim, profiles=profiles, registration=registration, changes=changes, usage=usage,work_collection=for_container(s,key),
                          locations=list(s.scalars(select(Location).order_by(Location.name))))

    @app.get("/containers/{key}/edit", response_class=HTMLResponse)
    def container_edit_page(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            container = get(s, Container, key)
            usage = container_usage(s, container)
            if container.deleted or container.archived or usage["locked"]:
                raise DomainError("Restore the container or finish its active job before editing")
            return render(request, "container_edit.html", db_session=s, user=user, container=container, usage=usage,
                          locations=list(s.scalars(select(Location).order_by(Location.name))))

    @app.get("/containers/{key}/delete", response_class=HTMLResponse)
    def container_delete_page(request: Request, key: str):
        user = user_for(request)
        require_role(user, "supervisor", "admin")
        with db.read() as s:
            container = s.get(Container, key)
            if container is None or container.deleted:
                return redirect("/containers/" + key)
            return render(request, "container_delete.html", db_session=s, user=user, container=container,
                          usage=container_usage(s, container))

    @app.post("/containers/{key}/{action}")
    async def container_change(request: Request, key: str, action: str):
        user = user_for(request)
        data = await form(request)
        if action == "edit":
            payload = {field: data.get(field) for field in ("label", "location", "location_id", "kind", "capacity", "tare_mass", "revision", "reason")}
            await run_in_threadpool(workflow.update_container, user.id, data["key"], key, payload)
        elif action == "move":
            await run_in_threadpool(workflow.move_container, user.id, data["key"], key,
                                   location_id=data.get("location_id"), revision=data.get("revision"))
            request.session["move_recorded"] = True
            return redirect("/handling/move")
        elif action in {"archive", "restore", "delete"}:
            await run_in_threadpool(workflow.retire_container, user.id, data["key"], key, revision=data.get("revision"),
                                   action=action, confirmed=data.get("confirmed") == "on", reason=data.get("reason", ""))
            if action == "delete":
                return redirect("/containers")
        else:
            raise HTTPException(404, "Unknown container action")
        return redirect("/containers/" + key)

    @app.get("/receive", response_class=HTMLResponse)
    def receive_page(request: Request):
        user = user_for(request)
        with db.read() as s:
            active = list(s.scalars(select(Container).where(Container.archived == False, Container.deleted == False).order_by(Container.label)))
            occupied = set(s.scalars(select(Assignment.container_id).where(Assignment.active == True)))
            reserved = set(s.scalars(select(Destination.container_id).where(Destination.active == True)))
            eligible = [c for c in active if c.id not in occupied | reserved]
            unavailable = [{"container": c, "reason": "Already contains material" if c.id in occupied else "Reserved for an active job"}
                           for c in active if c.id in occupied | reserved]
            return render(request, "receive.html", db_session=s, user=user, containers=eligible, unavailable=unavailable,
                          stations=list(s.scalars(select(Station).filter_by(kind="manual"))), today=now()[:10],
                          locations=list(s.scalars(select(Location).order_by(Location.name))))

    @app.post("/receive")
    async def receive(request: Request):
        user = user_for(request)
        data = await form(request)
        fields = ("receiving_load_id", "receiving_container_id", "receiving_net_mass", "receiving_location_id", "receiving_container_revision")
        if any(not isinstance(data.get(field), list) for field in fields):
            raise DomainError("Reload the receiving form and complete all four sections")
        count = len(data["receiving_load_id"])
        if not 1 <= count <= 20 or any(len(data[field]) != count for field in fields) or len(set(data["receiving_load_id"])) != count:
            raise DomainError("Each receiving container needs a net weight and storage location")
        for field in ("receiving_tare_mass", "receiving_capacity"):
            if not data.get(field): data[field] = [""] * count  # Previously opened forms with known weights still work.
            if len(data[field]) != count:
                raise DomainError("Each receiving container needs its own container-weight fields")
        tared = set(data.get("tared", []))
        loads = [{"container_id": data["receiving_container_id"][i], "net_mass": data["receiving_net_mass"][i],
                  "location_id": data["receiving_location_id"][i], "revision": data["receiving_container_revision"][i],
                  "tared": data["receiving_load_id"][i] in tared} for i in range(count)]
        for index, row in enumerate(loads):
            for field, name in (("receiving_tare_mass", "tare_mass"), ("receiving_capacity", "capacity")):
                if data[field][index] not in (None, ""):
                    row[name] = data[field][index]
        source = {field: data.get(field) for field in ("supplier", "supplier_lot", "origin", "bean_type", "purchase_date", "received_date", "notes")}
        key = await run_in_threadpool(workflow.receive_loads, user.id, data["key"], source, loads,
            station_id=data.get("station_id"), ready=data.get("ready") == "on", placed=data.get("placed") == "on")
        return redirect("/receipts/" + key)

    @app.get("/receipts/{key}", response_class=HTMLResponse)
    def receipt_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            receipt = get(s, Receipt, key)
            operation = get(s, Operation, receipt.operation_id)
            rows = []
            for link in s.scalars(select(Output).filter_by(operation_id=operation.id).order_by(Output.stream)):
                portion = get(s, Portion, link.portion_id)
                assignment = s.scalar(select(Assignment).filter_by(operation_id=operation.id, portion_id=portion.id))
                rows.append({"link": link, "portion": portion, "container": s.get(Container, assignment.container_id) if assignment else None})
            return render(request, "receipt.html", db_session=s, user=user, receipt=receipt, operation=operation, loads=rows,
                          history=list(s.scalars(select(Audit).filter_by(operation_id=operation.id).order_by(Audit.created_at))))

    def preparation_context(s, operation=None):
        from .references import retired_recipe_ids
        retired = retired_recipe_ids(s)
        selected = prepared_snapshot(s, operation) if operation else {}
        portions = []
        for portion in s.scalars(select(Portion).filter_by(status="stock", quality="released")):
            own = portion.id == selected.get("portion_id")
            if available(s, portion) > 0 or own:
                row = portion_view(s, portion)
                from .handling import latest_profile
                description = latest_profile(s, portion.id)
                density = Decimal(str(description.data["count_per_kg"])) if description and description.data.get("count_per_kg") and get(s, Operation, portion.created_by).kind != "receiving" else None
                row["population_mean_mg"] = str(Decimal(1000000)/density) if density and density.is_finite() and density > 0 else ""
                if own:
                    row["available"] = balance(s, portion.id)
                portions.append(row)
        method = "simulated" if settings.environment == "simulation" else "manual"
        stations = [station for station in s.scalars(select(Station)) if method in station.capabilities["execution_methods"]
                    and (station.kind != "manual" or station.id == selected.get("station_id"))]
        containers = []
        destination_contents = {}
        for container in s.scalars(select(Container).where(Container.archived == False, Container.deleted == False).order_by(Container.label)):
            from .collections import eligible_destination
            if eligible_destination(s, container, operation_id=operation.id if operation else None):
                containers.append(container)
                assigned = active_assignment(s, container_id=container.id)
                destination_contents[container.id] = {"mass_mg":balance(s,assigned.portion_id) if assigned else 0,
                                                       "portion_id":assigned.portion_id if assigned else None}
        simulation = current_settings(s) if operation is None else s.get(SimulationSettings, selected.get("simulation_settings_id")) if selected.get("simulation_settings_id") else None
        return {"portions": portions, "containers": containers, "destination_contents":destination_contents, "stations": stations,
                "versions": sorted((v for v in s.scalars(select(Version).order_by(Version.version.desc())) if v.id not in retired),
                                   key=lambda v: v.data.get("schema") != "beanosort-reference-recipe/v1"),
                "simulation": simulation_view(s, simulation), "editing": operation, "selected": selected,
                "planned_mass": kg(operation.planned_mg) if operation else "",
                "retired_recipe": bool(operation and operation.recipe_id in retired)}

    @app.get("/jobs/new", response_class=HTMLResponse)
    def prepare_page(request: Request, recipe_id: str = ""):
        user = user_for(request)
        machine_context = {"machine_catalog":None, "machine_error":None}
        if machine.enabled:
            try: machine_context["machine_catalog"] = machine.catalog()
            except Exception as exc: machine_context["machine_error"] = str(exc)
        with db.read() as s:
            context = preparation_context(s)
            if recipe_id:
                from .references import retired_recipe_ids
                if recipe_id in retired_recipe_ids(s): raise DomainError("This recipe is retired; choose a current sorting recipe")
                if get(s, Version, recipe_id).kind != "recipe": raise DomainError("Choose a sorting recipe")
                context["selected"]["recipe_id"] = recipe_id
            return render(request, "prepare.html", db_session=s, user=user, **context, **machine_context)

    def population_context(dataset, recipe, items, model, chosen, *, simulated=False):
        from .references import features, load_dataset, matches, population_report, recipe_definitions
        rows, _ = load_dataset(dataset)
        by_sequence = {row["bean_sequence"]: row for row in rows}
        if items is None:
            items = []
            for row in rows:
                values = features(row, recipe_definitions(dataset, recipe))
                selected = matches(values[recipe["feature"]], recipe)
                items.append({"payload": {"features": values, "reference_bean": {"bean_sequence": row["bean_sequence"]},
                    "decision": "unknown" if selected is None else "selected" if selected else "pass",
                    "action": "attempted" if selected else "not_issued"}, "simulated_destination": None})
        gallery = []
        for item in items:
            payload, destination = item["payload"], item.get("simulated_destination")
            if chosen == "targeted" and payload["decision"] != "selected": continue
            if chosen in {"selected", "pass_through"} and destination != chosen: continue
            row = by_sequence[payload["reference_bean"]["bean_sequence"]]
            gallery.append({"bean": row, "decision": payload["decision"], "destination": destination,
                            "missed": payload["decision"] == "selected" and destination == "pass_through"})
            if len(gallery) == 24: break
        return {"dataset": dataset, "report": population_report(items, dataset, recipe, model),
                "gallery": gallery, "chosen_population": chosen, "has_simulation": simulated,
                "capture_probability": model["probability"]}

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request):
        from .references import METRICS, retired_recipe_ids
        user = user_for(request)
        with db.read() as s:
            retired = retired_recipe_ids(s)
            return render(request, "recipes.html", db_session=s, user=user,
                          recipes=[v for v in s.scalars(select(Version).filter_by(kind="recipe").order_by(Version.created_at.desc())) if v.id not in retired],
                          datasets=list(s.scalars(select(ReferenceDataset).order_by(ReferenceDataset.created_at.desc()))), metrics=METRICS)

    @app.post("/recipes")
    async def recipe_create(request: Request):
        from .references import create_recipe
        user = user_for(request)
        data = await form(request)
        key = await run_in_threadpool(create_recipe, db, user.id, data["key"], data.get("name"),
            data.get("dataset_id"), data.get("feature"), data.get("relation"), data.get("sigma"))
        return redirect("/recipes/"+key)

    @app.get("/recipes/{key}", response_class=HTMLResponse)
    def recipe_page(request: Request, key: str, population: str = "targeted"):
        from .references import retired_recipe_ids
        user = user_for(request)
        with db.read() as s:
            recipe = get(s, Version, key)
            if recipe.kind != "recipe": raise DomainError("This version is not a sorting recipe")
            context = {}
            if recipe.data.get("schema") == "beanosort-reference-recipe/v1":
                from .references import validate_recipe
                dataset = validate_recipe(s, recipe.data)
                current = current_settings(s)
                model = get(s, Version, current.capture_model_id).data if current else CAPTURE_DEFAULT
                context = population_context(dataset, recipe.data, None, model,
                                             population if population in {"input","targeted"} else "targeted")
                context["inspection_url"] = f"/recipes/{key}/inspect" + (f"?capture_model_id={current.capture_model_id}" if current else "")
            return render(request, "recipe.html", db_session=s, user=user, recipe=recipe, retired=key in retired_recipe_ids(s), **context)

    @app.get("/recipes/{key}/inspect")
    def recipe_inspection(request: Request, key: str, feature: str, bucket: int, population: str = "input",
                          basis: str = "expected", page: int = 1, capture_model_id: str = ""):
        from .population_inspection import inspect_population
        from .references import validate_recipe
        user_for(request)
        with db.read() as s:
            recipe = get(s, Version, key)
            if recipe.kind != "recipe" or recipe.data.get("schema") != "beanosort-reference-recipe/v1":
                raise DomainError("Choose a photographed-reference recipe")
            dataset = validate_recipe(s, recipe.data)
            model = get(s, Version, capture_model_id) if capture_model_id else None
            if model and model.kind != "capture_model":
                raise DomainError("Choose a valid capture-model version")
            return JSONResponse(inspect_population(s, dataset, recipe.data, model.data if model else CAPTURE_DEFAULT,
                feature=feature, bucket=bucket, population=population, basis=basis, page=page), headers={"Cache-Control": "no-store"})

    @app.get("/reference-datasets/{key}/photos/{sequence}/{camera}")
    def reference_photo(request: Request, key: str, sequence: int, camera: str):
        from .references import photo_path
        user_for(request)
        with db.read() as s:
            return FileResponse(photo_path(get(s, ReferenceDataset, key), sequence, camera))

    @app.get("/jobs/{key}/populations", response_class=HTMLResponse)
    def job_populations(request: Request, key: str, population: str = "input", contents: str = "", collection_id: str = ""):
        from .references import validate_recipe
        user = user_for(request)
        with db.read() as s:
            operation = get(s, Operation, key)
            recipe = get(s, Version, operation.recipe_id)
            if recipe.data.get("schema") != "beanosort-reference-recipe/v1":
                raise DomainError("This job uses the legacy fixture; prepare a reference-data recipe to compare photographed populations")
            dataset = validate_recipe(s, recipe.data)
            run = s.scalar(select(Run).filter_by(operation_id=key))
            if contents:
                from .collections import collection_report
                from .references import load_dataset
                frozen=s.scalar(select(Profile).filter_by(operation_id=key,portion_id=contents))
                collection=frozen.data.get("combined_population") if frozen else None
                if not collection:raise DomainError("This contents label has no compatible combined population analysis")
                beans,_=load_dataset(dataset);sequences={seq for seq,_ in collection["weights"]}
                context={"dataset":dataset,"report":collection_report(collection,dataset,recipe.data,get(s,Version,operation.capture_model_id).data),
                    "gallery":[{"bean":bean,"decision":"combined estimate","destination":None} for bean in beans if bean["bean_sequence"] in sequences][:24],
                    "chosen_population":"selected","has_simulation":False,"capture_probability":get(s,Version,operation.capture_model_id).data["probability"],
                    "inspection_url":f"/jobs/{key}/populations/inspect?contents={contents}","contents_id":contents}
                return render(request,"populations.html", db_session=s,user=user,recipe=recipe,operation=operation,run=None,**context)
            from .models import SortingCollection
            from .segments import particle_filter
            sort_collection=get(s,SortingCollection,collection_id) if collection_id else None
            if sort_collection and sort_collection.operation_id!=key:raise DomainError("Collection belongs to another job")
            query=select(Particle).filter_by(run_id=run.id).order_by(Particle.source_sequence) if run else None
            if sort_collection:
                condition=particle_filter(s,sort_collection)
                if condition is None:raise DomainError("This collection has no reliable local particle association")
                if query is not None:query=query.where(condition)
            items = [{"payload": p.payload, "simulated_destination": p.simulated_destination} for p in s.scalars(query)] if query is not None else []
            context = population_context(dataset, recipe.data, items, get(s, Version, operation.capture_model_id).data,
                                         population if population in {"input","targeted","selected","pass_through"} else "input", simulated=True)
            frozen = s.scalar(select(Profile).filter_by(operation_id=key,stream=sort_collection.code if sort_collection else "run"))
            if frozen:
                context["report"] = frozen.data["reference_population"]
            context["inspection_url"] = f"/jobs/{key}/populations/inspect"+(f"?collection_id={collection_id}" if sort_collection else "")
            context["sorting_collection"]=sort_collection
            context["collection_container"]=get(s,Container,sort_collection.container_id) if sort_collection else None
            from .population_sources import input_profile
            context["population_input"] = input_profile(s, key)
            weighed_profile = s.scalar(select(Profile).filter_by(operation_id=key, stream=sort_collection.code if sort_collection else context["chosen_population"]))
            context["weighing_estimate"] = weighed_profile.data.get("mass_count_estimate") if weighed_profile else None
            return render(request, "populations.html", db_session=s, user=user, recipe=recipe, operation=operation, run=run, **context)

    @app.get("/jobs/{key}/populations/inspect")
    def job_population_inspection(request: Request, key: str, feature: str, bucket: int,
                                 population: str = "input", basis: str = "expected", page: int = 1, contents: str = "", collection_id: str = ""):
        from .population_inspection import inspect_population
        from .references import validate_recipe
        user_for(request)
        with db.read() as s:
            operation = get(s, Operation, key)
            recipe = get(s, Version, operation.recipe_id)
            if operation.kind != "sorting" or recipe.data.get("schema") != "beanosort-reference-recipe/v1":
                raise DomainError("Choose a photographed-reference sorting job")
            dataset = validate_recipe(s, recipe.data)
            if contents:
                frozen=s.scalar(select(Profile).filter_by(operation_id=key,portion_id=contents))
                collection=frozen.data.get("combined_population") if frozen else None
                if not collection:raise DomainError("No compatible combined population exists for this contents label")
                return JSONResponse(inspect_population(s,dataset,recipe.data,get(s,Version,operation.capture_model_id).data,
                    feature=feature,bucket=bucket,population=population,basis=basis,page=page,collection=collection),headers={"Cache-Control":"no-store"})
            run = s.scalar(select(Run).filter_by(operation_id=key))
            if not run:
                raise DomainError("Start and ingest the simulation before inspecting run passages")
            from .models import SortingCollection
            sort_collection=get(s,SortingCollection,collection_id) if collection_id else None
            if sort_collection and sort_collection.operation_id!=key:raise DomainError("Collection belongs to another job")
            return JSONResponse(inspect_population(s, dataset, recipe.data, get(s, Version, operation.capture_model_id).data,
                feature=feature, bucket=bucket, population=population, basis=basis, page=page, run_id=run.id,sort_collection=sort_collection), headers={"Cache-Control": "no-store"})

    @app.get("/jobs/{key}/edit", response_class=HTMLResponse)
    def edit_prepared_page(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            operation = get(s, Operation, key)
            require_unstarted_sorting(s, operation)
            return render(request, "prepare.html", db_session=s, user=user, **preparation_context(s, operation))

    @app.get("/jobs/{key}/change-containers", response_class=HTMLResponse)
    def change_containers_page(request: Request, key: str):
        from .collections import eligible_destination,feed_input
        from .segments import current,collections,has_capacity
        from .models import SortingCollection, MachineJob
        from .sort_capacity import snapshot as capacity_snapshot
        from .sorting_modes import hopper_mode
        user=user_for(request)
        with db.read() as s:
            operation=get(s,Operation,key)
            machine_job=s.get(MachineJob,key)
            if machine_job and (machine_job.state!='paused' or not machine_job.status.get('pause_drained') or machine_job.cursor<machine_job.status.get('cursor',0)):
                raise DomainError("Wait for the paused machine to drain and synchronize before changing containers")
            if operation.kind!="sorting" or operation.status!="paused":raise DomainError("Pause this sorting job before changing containers")
            source=active_assignment(s,portion_id=feed_input(s,operation).portion_id)
            if not source:raise DomainError("Source assignment unavailable")
            segment=current(s,operation);items=collections(s,operation)
            used={item.container_id for item in items};hopper=hopper_mode(s,operation)
            if segment:
                bindings={stream:get(s,SortingCollection,getattr(segment,stream+"_id")).container_id
                          for stream in ("selected","pass_through") if getattr(segment,stream+"_id")}
            else:bindings={d.stream:d.container_id for d in s.scalars(select(Destination).filter_by(operation_id=key))}
            candidates=[c for c in s.scalars(select(Container).where(Container.deleted==False,Container.archived==False).order_by(Container.label))
                if c.id not in used and c.id not in bindings.values() and
                   (eligible_destination(s,c) or hopper and c.id==source.container_id)]
            return render(request,"change_containers.html",db_session=s,user=user,op=operation,
                recipe=get(s,Version,operation.recipe_id),source_container=get(s,Container,source.container_id),
                bindings={stream:get(s,Container,cid) for stream,cid in bindings.items()},candidates=candidates,
                hopper_emptying=hopper,simulation=settings.environment=="simulation",machine_job=machine_job,
                capacity_rows=capacity_snapshot(s,operation,settings.environment))

    @app.post("/collections/{key}/weigh")
    async def weigh_closed_collection(request: Request,key:str):
        from .segments import record_weight
        from .models import SortingCollection
        user=user_for(request);data=await form(request)
        with db.read() as s:operation_id=get(s,SortingCollection,key).operation_id
        await run_in_threadpool(record_weight,workflow,user.id,data["key"],key,
            {"gross":data.get("gross"),"revision":data.get("revision"),"weighed":data.get("weighed")=="on","reason":data.get("reason")})
        return redirect("/jobs/"+operation_id+"#"+key)

    @app.get("/collections/{key}")
    def collection_detail(request:Request,key:str):
        from .models import SortingCollection
        user_for(request)
        with db.read() as s:operation_id=get(s,SortingCollection,key).operation_id
        return redirect("/jobs/"+operation_id+"#"+key)

    @app.post("/jobs")
    async def prepare(request: Request):
        user = user_for(request)
        data = await form(request)
        payload = {k: data.get(k, "") for k in ("portion_id", "planned_mass", "station_id", "selected_id", "pass_through_id", "recipe_id", "policy_id")}
        for field in ("capture_model_id", "simulation_settings_id"):
            if data.get(field):
                payload[field] = data[field]
        payload["ready"] = data.get("ready") == "on"
        from .sorting_modes import is_machine_recipe
        with db.read() as s:
            use_machine = is_machine_recipe(get(s, Version, payload["recipe_id"]).data)
        if use_machine:
            payload["selected_id"] = None
            payload["machine"] = await run_in_threadpool(machine.prepare_manifest, data)
        key = await run_in_threadpool(workflow.prepare, user.id, data["key"], payload)
        return redirect("/jobs/" + key)

    @app.get("/jobs/{key}", response_class=HTMLResponse)
    def job(request: Request, key: str):
        from .references import retired_recipe_ids
        user = user_for(request)
        with db.read() as s:
            op = get(s, Operation, key)
            if op.kind != "sorting":
                raise DomainError("This operation is not a sorting job")
            run = s.scalar(select(Run).filter_by(operation_id=key))
            from .models import MachineJob
            machine_job = s.get(MachineJob, key)
            from .collections import baselines, feed_input, starting_mass
            source = feed_input(s, op)
            outputs = [{"link": o, **portion_view(s, get(s, Portion, o.portion_id))} for o in effective_outputs(s, op)]
            frozen = s.scalar(select(Profile).filter_by(operation_id=key, stream="run"))
            profile = frozen.data if frozen else workflow.profile(s, op)
            assumption = s.scalar(select(SimulationAssumption).filter_by(operation_id=key))
            from .population_sources import input_profile
            population_input = input_profile(s, key)
            destinations = [{"stream": d.stream, "container": get(s, Container, d.container_id)} for d in s.scalars(select(Destination).filter_by(operation_id=key))]
            from .sorting_modes import hopper_mode, source_return
            from .segments import current
            from .collection_views import rows as collection_view_rows
            from .sort_capacity import snapshot as capacity_snapshot
            segment=current(s,op);collection_rows=collection_view_rows(s,op)
            if segment:
                destinations=[{"stream":row["collection"].stream,"container":row["container"]} for row in collection_rows if row["open"]]
            return render(request, "job.html", db_session=s, user=user, op=op, run=run, machine_job=machine_job, source=portion_view(s, get(s, Portion, source.portion_id)),
                          outputs=outputs, destinations=destinations, profile=profile, station=get(s, Station, op.station_id),
                          station_fresh=bool(get(s, Station, op.station_id).last_received_at and
                              (datetime.now(timezone.utc) - datetime.fromisoformat(get(s, Station, op.station_id).last_received_at)).total_seconds() < settings.station_stale_seconds),
                          station_age=age_seconds(get(s, Station, op.station_id).last_received_at),
                          recipe=get(s, Version, op.recipe_id), recipe_retired=op.recipe_id in retired_recipe_ids(s), policy=get(s, Version, op.policy_id),
                          recon=effective_reconciliation(s, op),
                          count_estimate=None if machine_job else assumption.data if assumption else population_input.data.get("count_basis") if population_input else None,
                          population_input=population_input, max_particles=MAX_SIMULATION_PARTICLES,
                          destination_baselines=baselines(s,op), accounting_input_mg=op.boundary_mg+starting_mass(s,op),
                          hopper_emptying=hopper_mode(s,op), source_return=source_return(s,op),
                          segmented_job=bool(segment),collection_rows=collection_rows,
                          capacity_rows=capacity_snapshot(s,op,settings.environment),
                          distribution_url="/jobs/"+key+"/distributions",
                          collection_weigh_count=len(collection_rows)+(0 if any(row["source_alias"] for row in collection_rows) else 1),
                          can_manage_prepared=op.status == "prepared" and run is None and not machine_job,
                          removed=s.scalar(select(Audit).where(Audit.operation_id == key, Audit.action.in_(["job_deleted", "job_replaced"]))),
                          replaces=s.scalar(select(Audit).filter_by(operation_id=key, action="job_replacement_created")),
                          history=list(s.scalars(select(Audit).filter_by(operation_id=key).order_by(Audit.created_at))))

    @app.get("/jobs/{key}/capacity-status")
    def capacity_status(request: Request, key: str):
        from .sort_capacity import snapshot
        user_for(request)
        with db.read() as s:
            operation=get(s,Operation,key)
            if operation.kind != "sorting":raise DomainError("Choose a sorting job")
            return JSONResponse({"status":operation.status,"receivers":snapshot(s,operation,settings.environment)},
                                headers={"Cache-Control":"no-store"})

    @app.get("/jobs/{key}/distributions", response_class=HTMLResponse)
    def job_distributions(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            operation = get(s, Operation, key)
            if operation.kind != "sorting": raise DomainError("Choose a sorting job")
            frozen = s.scalar(select(Profile).filter_by(operation_id=key, stream="run"))
            profile = frozen.data if frozen else workflow.profile(s, operation)
            template = "job_distributions_fragment.html" if request.headers.get("X-BeanoSort-Fragment") == "1" else "job_distributions.html"
            response = render(request, template, user=user, op=operation, profile=profile)
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.get("/jobs/{key}/weigh", response_class=HTMLResponse)
    def weigh_job(request: Request, key: str):
        from .weighing import weighing_context
        user = user_for(request)
        with db.read() as s:
            operation = get(s, Operation, key)
            return render(request, "weigh.html", db_session=s, user=user, op=operation,
                          **weighing_context(s, operation, settings.environment))

    @app.post("/jobs/{key}/weigh/review", response_class=HTMLResponse)
    async def review_weighing(request: Request, key: str):
        from .weighing import build_review
        user = user_for(request)
        values = await form(request)
        def review():
            with db.write() as s:
                operation = get(s, Operation, key)
                context = build_review(s, operation, settings.environment, values)
                if operation.status == "finishing":
                    require_role(user, "operator", "supervisor", "admin")
                    # Every final-weighing confirmation also confirms flow has
                    # stopped. Do not advance on GET or invalid/partial weights.
                    operation.status = "awaiting_reconciliation"
                    audit(s, user, "execution_transition", {
                        "from": "finishing", "to": "awaiting_reconciliation",
                        "reason": "Final container weights confirmed after processing stopped and in-flight material was accounted for",
                        "in_flight_accounted_for": True, "confirmed_with": "final_container_weighings",
                    }, operation)
                return operation, context
        operation, context = await run_in_threadpool(review)
        context["data"]["evidence"]["entered_by"] = user.id
        context["data"]["capacity_override"] = True  # Effective only after explicit acceptance of the signed review.
        token = weighing_signer.dumps({"operation_id": key, "operator_id": user.id, "key": values["key"], "data": context["data"]})
        return render(request, "weigh_review.html", user=user, op=operation, review_token=token, **context)

    @app.post("/jobs/{key}/weigh/accept")
    async def accept_weighing(request: Request, key: str):
        user = user_for(request)
        values = await form(request)
        if values.get("accept_review") != "on":
            raise DomainError("Accept the reviewed container weights and signed discrepancy first")
        try:
            review = weighing_signer.loads(values.get("review_token", ""), max_age=3600)
        except BadSignature:
            raise DomainError("This weighing review expired or changed; review the container weights again") from None
        if review.get("operation_id") != key or review.get("operator_id") != user.id:
            raise DomainError("This weighing review belongs to a different job")
        await run_in_threadpool(workflow.reconcile, user.id, review["key"], key, review["data"])
        if user.role in {"admin", "supervisor"}:
            await run_in_threadpool(workflow.release, user.id, key, accept_estimates=True,
                reason=values.get("reason") or "Accepted reviewed container weights and their signed discrepancy",
                approve_discrepancy=settings.environment == "simulation")
        return redirect("/jobs/"+key)

    @app.post("/jobs/{key}/{action}")
    async def job_action(request: Request, key: str, action: str):
        user = user_for(request)
        data = await form(request)
        with db.read() as s:
            if get(s, Operation, key).kind != "sorting":
                raise DomainError("Use the corresponding manual operation workflow")
            from .models import MachineJob
            machine_job = s.get(MachineJob, key)
            if machine_job and action in {"start","transition","simulate","simulate-batch","replay","import","edit","pause-for-change"}:
                raise DomainError("Use this job's machine controls; its execution and evidence come from the Jetson")
            if machine_job and action == "change-containers":
                if machine_job.state != "paused" or not machine_job.status.get("pause_drained") or machine_job.cursor < machine_job.status.get("cursor",0):
                    raise DomainError("Wait for the machine pause to drain and its numerical journal to synchronize")
                data["first_sequence"] = str(machine_job.status.get("metrics",{}).get("tracked_beans",0))
        if action == "edit":
            payload = {field: data.get(field) for field in ("portion_id", "planned_mass", "station_id", "selected_id", "pass_through_id", "recipe_id", "policy_id")}
            for field in ("capture_model_id", "simulation_settings_id"):
                if data.get(field):
                    payload[field] = data[field]
            payload["ready"] = data.get("ready") == "on"
            replacement = await run_in_threadpool(workflow.edit_prepared_sorting, user.id, data["key"], key, payload,
                                                  confirm_unstarted=data.get("confirm_unstarted") == "on")
            return redirect("/jobs/" + replacement)
        elif action == "delete":
            await run_in_threadpool(workflow.delete_prepared_sorting, user.id, data["key"], key,
                                    confirm_unstarted=data.get("confirm_unstarted") == "on")
            return redirect("/")
        elif action == "pause-for-change":
            await run_in_threadpool(workflow.transition,user.id,key,"paused",reason="Pause for output container change")
            return redirect("/jobs/"+key+"/change-containers")
        elif action == "change-containers":
            from .segments import change_containers
            await run_in_threadpool(change_containers,workflow,user.id,data["key"],key,
                {"selected_id":data.get("selected_id"),"pass_through_id":data.get("pass_through_id"),
                 "feeding_stopped":data.get("feeding_stopped")=="on","in_flight_clear":data.get("in_flight_clear")=="on",
                 "ready":data.get("ready")=="on","reason":data.get("reason"),
                 "capacity_override":data.get("capacity_override")=="on",
                 "first_sequence":data.get("first_sequence"),"source_at":data.get("source_at")})
        elif action == "start":
            try:
                count = int(data["count"]) if data.get("count") else None
            except ValueError:
                raise DomainError("Particle count must be an integer") from None
            await run_in_threadpool(workflow.start, user.id, key, seed=data.get("seed", "42"), count=count, source_run_id=data.get("source_run_id"))
        elif action in {"simulate", "simulate-batch", "replay"}:
            with db.read() as s:
                run_id = s.scalar(select(Run.id).filter_by(operation_id=key))
                if run_id is None:
                    raise DomainError("Start the workflow run first")
            inserted = await run_in_threadpool(workflow.simulate_next, user.id, run_id, replay_first=action == "replay")
            if action == "simulate-batch":
                with db.read() as s:
                    run = get(s, Run, run_id)
                    from .run_summaries import get_summary
                    summary = get_summary(s, run_id)
                    total = summary.particle_count if summary else s.scalar(select(func.count(Particle.id)).filter_by(run_id=run_id))
                    return JSONResponse({"inserted": inserted, "received": total, "expected": run.expected_particles,
                                         "complete": total == run.expected_particles, "evidence": "simulation",
                                         "paused":get(s,Operation,key).status == "paused"})
        elif action == "reconcile":
            payload = {k: data.get(k) for k in ("selected", "pass_through", "remainder", "retained", "loss", "sample", "reason")}
            payload["evidence"] = evidence(data)
            await run_in_threadpool(workflow.reconcile, user.id, data["key"], key, payload)
        elif action == "correct":
            payload = {k: data.get(k) for k in ("selected", "pass_through", "remainder", "retained", "loss", "sample", "reason")}
            payload["evidence"] = evidence(data)
            await run_in_threadpool(handling.correct_sorting, user.id, data["key"], key, payload)
        elif action == "import":
            from .adapter import import_spool
            require_role(user, "supervisor", "admin")
            with db.read() as s:
                run_id = s.scalar(select(Run.id).filter_by(operation_id=key))
            if not run_id:
                raise DomainError("Associate the source run first")
            try:
                await run_in_threadpool(import_spool, workflow, user.id, run_id, data["directory"])
            except (OSError, ValueError, KeyError) as exc:
                raise DomainError("Cannot read a valid offline spool; inspect the directory and manifest") from exc
        elif action == "release":
            await run_in_threadpool(workflow.release, user.id, key, accept_estimates=data.get("accept_estimates") == "on",
                                    reason=data.get("reason", ""), approve_discrepancy=data.get("accept_discrepancy") == "on")
        elif action == "transition":
            await run_in_threadpool(workflow.transition, user.id, key, data["target"], reason=data.get("reason", ""), drained=data.get("drained") == "on")
            if data["target"] in {"finishing", "awaiting_reconciliation"}:
                return redirect("/jobs/" + key + "/weigh")
        else:
            raise HTTPException(404, "Unknown action")
        return redirect("/jobs/" + key)

    @app.get("/simulation-settings", response_class=HTMLResponse)
    def simulation_settings_page(request: Request):
        user = user_for(request)
        with db.read() as s:
            history = [simulation_view(s, x) for x in s.scalars(select(SimulationSettings).order_by(SimulationSettings.version.desc()))]
            return render(request, "simulation_settings.html", db_session=s, user=user, current=history[0] if history else None,
                          settings_history=history, research=RESEARCH, research_note=RESEARCH_NOTE)

    @app.post("/simulation-settings")
    async def simulation_settings_save(request: Request):
        user = user_for(request)
        data = await form(request)
        payload = {k: data.get(k) for k in ("previous_id", "material_label", "mean_mass_g", "capture_percent", "reason")}
        await run_in_threadpool(simulation_config.save, user.id, data["key"], payload)
        return redirect("/simulation-settings")

    def retained_portion(s, key):
        from .handling import latest_profile
        portion = get(s, Portion, key)
        profile = latest_profile(s, key)
        population = profile.data.get("retained_population") if profile else None
        if not population:
            raise DomainError("This portion has no retained joint population to inspect")
        return portion, profile, population

    @app.get("/portions/{key}/populations", response_class=HTMLResponse)
    def portion_populations(request: Request, key: str):
        from .retained_populations import members, population_report
        user = user_for(request)
        with db.read() as s:
            portion, profile, population = retained_portion(s, key)
            rows = members(s, population)
            datasets = {row["dataset"].id: row["dataset"] for row in rows}
            return render(request, "portion_populations.html", db_session=s, user=user, portion=portion,
                          profile=profile, population=population, report=population_report(s, population),
                          gallery=rows[:24], member_count=len(rows), datasets=list(datasets.values()),
                          inspection_url=f"/portions/{key}/populations/inspect")

    @app.get("/portions/{key}/populations/inspect")
    def portion_population_inspection(request: Request, key: str, feature: str, bucket: int,
                                      population: str = "input", basis: str = "expected", page: int = 1):
        from .retained_populations import inspect_population
        user_for(request)
        if population != "input" or basis != "expected":
            raise DomainError("Choose the retained contents population; no new sorting outcome is asserted")
        with db.read() as s:
            _, _, retained = retained_portion(s, key)
            return JSONResponse(inspect_population(s, retained, feature=feature, bucket=bucket, page=page),
                                headers={"Cache-Control": "no-store"})

    @app.get("/portions/{key}", response_class=HTMLResponse)
    def portion_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            portion = get(s, Portion, key)
            related, edges = trace(s, key)
            related_ids = [p.id for p in related]
            operations = {p.created_by for p in related}
            operations |= set(s.scalars(select(Input.operation_id).where(Input.portion_id.in_(related_ids))))
            return render(request, "portion.html", db_session=s, user=user, current=portion_view(s, portion),
                          product=s.scalar(select(ProductLot).filter_by(portion_id=key)),
                          stations=list(s.scalars(select(Station).filter_by(kind="manual"))),
                          profiles=list(s.scalars(select(Profile).filter_by(portion_id=key).order_by(Profile.created_at.desc(), Profile.version.desc()))),
                          related=[portion_view(s, p) for p in related], edges=edges,
                          measurements=list(s.scalars(select(Measurement).where(Measurement.operation_id.in_(operations)))),
                          movements=list(s.scalars(select(Ledger).filter_by(portion_id=key).order_by(Ledger.created_at))),
                          reservation=s.scalar(select(Reservation).filter_by(portion_id=key, active=True)),
                          history=list(s.scalars(select(Audit).where(Audit.operation_id.in_(operations)).order_by(Audit.created_at))))

    @app.post("/portions/{key}/hold")
    async def hold(request: Request, key: str):
        user = user_for(request)
        data = await form(request)
        await run_in_threadpool(workflow.set_hold, user.id, key, data.get("reason", ""))
        return redirect("/portions/" + key)

    @app.post("/portions/{key}/clear-hold")
    async def clear_hold(request: Request, key: str):
        user = user_for(request)
        data = await form(request)
        await run_in_threadpool(handling.clear_hold, user.id, key, data.get("reason", ""))
        return redirect("/portions/" + key)

    @app.post("/portions/{key}/count-sample")
    async def count_sample(request: Request, key: str):
        user = user_for(request)
        data = await form(request)
        try:
            count = int(data.get("sample_count", ""))
        except ValueError:
            raise DomainError("Enter an integer sampled bean count") from None
        payload = {"station_id": data["station_id"], "evidence": evidence(data),
                   "sample_returned": data.get("sample_returned") == "on", "representative": data.get("representative") == "on"}
        await run_in_threadpool(handling.count_sample, user.id, data["key"], key, count, data.get("sample_mass", ""), payload)
        return redirect("/portions/" + key)

    @app.get("/operations/{key}")
    def operation_link(request: Request, key: str):
        user_for(request)
        with db.read() as s:
            op = get(s, Operation, key)
            if op.kind == "sorting":
                return redirect("/jobs/" + key)
            if s.scalar(select(HandlingPlan.id).filter_by(operation_id=key)):
                return redirect("/handling/" + key)
            linked = s.scalar(select(Output.portion_id).filter_by(operation_id=key)) or s.scalar(select(Input.portion_id).filter_by(operation_id=key))
            if linked:
                return redirect("/portions/" + linked)
            raise DomainError("Operation has no material links")

    @app.get("/handling", response_class=HTMLResponse)
    def handling_index(request: Request):
        user = user_for(request)
        with db.read() as s:
            entries = list(s.execute(select(Operation, HandlingPlan).join(HandlingPlan, HandlingPlan.operation_id == Operation.id).order_by(Operation.created_at.desc())))
            jobs = [operation for operation, _ in entries]
            moves = list(s.scalars(select(Audit).filter_by(action="container_moved").order_by(Audit.created_at.desc()).limit(10)))
            records = {operation.id: plan.data for operation, plan in entries}
            return render(request, "handling_index.html", db_session=s, user=user, jobs=jobs, moves=moves, records=records)

    @app.get("/handling/move", response_class=HTMLResponse)
    def handling_move_page(request: Request, container_id: str = ""):
        user = user_for(request)
        with db.read() as s:
            rows = [{"container": c, "usage": container_usage(s, c)} for c in s.scalars(select(Container).where(
                Container.archived == False, Container.deleted == False).order_by(Container.label))]
            selected = None
            current = None
            usage = None
            if container_id:
                selected = get(s, Container, container_id)
                if selected.deleted or selected.archived:
                    raise DomainError("Restore an archived container before moving it; deleted containers cannot be moved")
                usage = container_usage(s, selected)
                assignment = active_assignment(s, container_id=selected.id)
                if assignment:
                    current = portion_view(s, get(s, Portion, assignment.portion_id))
            response = render(request, "handling_move.html", db_session=s, user=user, containers=rows, selected=selected,
                              current=current, usage=usage, locations=list(s.scalars(select(Location).order_by(Location.name))),
                              move_recorded=request.session.pop("move_recorded", False))
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.post("/handling/move")
    async def handling_move_record(request: Request):
        user = user_for(request)
        data = await form(request)
        if data.get("physical_confirmation") != "on":
            raise DomainError("Confirm this container has been moved to the selected location")
        container_id = data.get("container_id")
        await run_in_threadpool(workflow.move_container, user.id, data["key"], container_id,
                               location_id=data.get("location_id"), revision=data.get("revision"))
        request.session["move_recorded"] = True
        return redirect("/handling/move")

    def material_form(request, user, kind, selected=""):
        from .material import container_state, eligible, packaging_eligible
        with db.read() as s:
            rows = []
            for container in s.scalars(select(Container).where(Container.archived == False, Container.deleted == False).order_by(Container.label)):
                state = container_state(s, container)
                rows.append({"container": container, "state": state, "source_eligible": eligible(state, source=True),
                             "eligible": eligible(state), "packaging_eligible": packaging_eligible(state),
                             "token": material_signer.dumps({"operator_id": user.id, "state": state})})
            template = "material_package.html" if kind == "product_packaging" else "material_form.html"
            response = render(request, template, db_session=s, user=user, kind=kind, rows=rows, selected=selected)
            response.headers["Cache-Control"] = "no-store"
            return response

    def material_state(values, container_id, user):
        try:
            signed = material_signer.loads(values.get("state_" + str(container_id), ""), max_age=8*60*60)
        except BadSignature:
            raise DomainError("The container choices expired or changed; reload the form") from None
        if signed.get("operator_id") != user.id or signed.get("state", {}).get("container_id") != container_id:
            raise DomainError("These container choices belong to another form; reload before saving")
        return signed["state"]

    @app.get("/handling/contents", response_class=HTMLResponse)
    def move_contents_page(request: Request):
        return material_form(request, user_for(request), "contents_move")

    @app.post("/handling/contents")
    async def move_contents_record(request: Request):
        user = user_for(request)
        values = await form(request)
        data = {field: values.get(field, "") for field in ("source_id", "destination_id", "moved_mass", "reason")}
        data.update(source_state=material_state(values, data["source_id"], user),
                    destination_state=material_state(values, data["destination_id"], user),
                    empty_source=values.get("empty_source") == "on", representative=values.get("representative") == "on", confirmed=True)
        key = await run_in_threadpool(material_actions.move_contents, user.id, values["key"], data)
        return redirect("/handling/" + key)

    @app.get("/handling/reconcile", response_class=HTMLResponse)
    def adjust_weight_page(request: Request, container_id: str = ""):
        return material_form(request, user_for(request), "weight_adjustment", container_id)

    @app.post("/handling/reconcile")
    async def adjust_weight_record(request: Request):
        user = user_for(request)
        values = await form(request)
        data = {field: values.get(field, "") for field in ("container_id", "weight", "weighing_basis", "reason")}
        data.update(container_state=material_state(values, data["container_id"], user),
                    representative=values.get("representative") == "on", confirmed=True)
        key = await run_in_threadpool(material_actions.reconcile, user.id, values["key"], data)
        return redirect("/handling/" + key)

    @app.get("/handling/package", response_class=HTMLResponse)
    def package_product_page(request: Request, container_id: str = ""):
        return material_form(request, user_for(request), "product_packaging", container_id)

    @app.post("/handling/package")
    async def package_product_record(request: Request):
        user = user_for(request)
        values = await form(request)
        data = {field: values.get(field, "") for field in ("container_id", "weight", "weighing_basis", "reason")}
        data.update(container_state=material_state(values, data["container_id"], user),
                    representative=values.get("representative") == "on", confirmed=True)
        key = await run_in_threadpool(material_actions.package_product, user.id, values["key"], data)
        return redirect("/handling/" + key)

    @app.get("/handling/new", response_class=HTMLResponse)
    def handling_prepare(request: Request, kind: str = "split"):
        user = user_for(request)
        if kind not in {"transfer", "split", "blend", "packaging"}:
            raise DomainError("Unknown material operation")
        return redirect("/handling/package" if kind == "packaging" else "/handling/contents")

    @app.post("/handling")
    async def handling_create(request: Request):
        user = user_for(request)
        data = await form(request)
        raise DomainError("Use Move contents or Package as product for new material work")

    @app.get("/handling/{key}", response_class=HTMLResponse)
    def handling_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            op = get(s, Operation, key)
            plan = s.scalar(select(HandlingPlan).filter_by(operation_id=key))
            if not plan:
                raise DomainError("This operation has no manual handling plan")
            if plan.data.get("schema") == "beanosort-material-record/v1":
                template = "material_product_record.html" if op.kind == "product_packaging" else "material_record.html"
                return render(request, template, db_session=s, user=user, op=op, record=plan.data)
            return render(request, "handling_job.html", db_session=s, user=user, op=op, plan=plan,
                          sources=[{"input": i, **portion_view(s, get(s, Portion, i.portion_id))} for i in s.scalars(select(Input).filter_by(operation_id=key))],
                          destination=get(s, Container, plan.data["destination_id"]), station=get(s, Station, op.station_id),
                          spec=s.get(Version, plan.specification_id) if plan.specification_id else None,
                          recon=effective_reconciliation(s, op), policy=get(s, Version, op.policy_id),
                          outputs=[{"link": o, **portion_view(s, get(s, Portion, o.portion_id))} for o in effective_outputs(s, op)],
                          product=s.scalar(select(ProductLot).filter_by(operation_id=key)),
                          history=list(s.scalars(select(Audit).filter_by(operation_id=key).order_by(Audit.created_at))))

    @app.post("/handling/{key}/{action}")
    async def handling_action(request: Request, key: str, action: str):
        user = user_for(request)
        data = await form(request)
        if action == "start":
            await run_in_threadpool(handling.start, user.id, key)
        elif action == "execute":
            payload = {k: data.get(k) for k in ("output_mass", "remainder", "loss", "reason")}
            payload.update(evidence=evidence(data), physical_confirmation=data.get("physical_confirmation") == "on",
                           representative=data.get("representative") == "on",
                           pack_masses=data.get("pack_masses", "").split(),
                           actual_inputs={k[7:]: v for k, v in data.items() if k.startswith("actual_")})
            await run_in_threadpool(handling.execute, user.id, data["key"], key, payload)
        elif action == "release":
            await run_in_threadpool(workflow.release, user.id, key, accept_estimates=data.get("accept_estimates") == "on", reason=data.get("reason", ""))
        elif action == "transition":
            await run_in_threadpool(workflow.transition, user.id, key, data.get("target"), reason=data.get("reason", ""), drained=data.get("drained") == "on")
        else:
            raise HTTPException(404, "Unknown manual action")
        return redirect("/handling/" + key)

    @app.get("/specifications", response_class=HTMLResponse)
    def specifications(request: Request):
        user = user_for(request)
        with db.read() as s:
            return render(request, "specifications.html", db_session=s, user=user,
                          versions=list(s.scalars(select(Version).where(Version.kind.in_(["product_specification", "packaging_specification"])).order_by(Version.created_at.desc()))))

    @app.post("/specifications")
    async def specification_create(request: Request):
        user = user_for(request)
        require_role(user, "admin")
        data = await form(request)
        if data["kind"] == "packaging_specification":
            payload = {"sku": data["sku"], "nominal_mg": mass(data["nominal_mass"])}
        else:
            try:
                constraints = json.loads(data.get("constraints", "[]"))
            except ValueError:
                raise DomainError("Enter a JSON array of constraints") from None
            if not isinstance(constraints, list) or any(not isinstance(c, dict) for c in constraints):
                raise DomainError("Enter a JSON array of constraint objects")
            payload = {"constraints": constraints}
        await run_in_threadpool(workflow.create_version, user.id, data["kind"], data["name"], payload)
        return redirect("/specifications")

    @app.get("/products/{key}", response_class=HTMLResponse)
    def product_detail(request: Request, key: str):
        user = user_for(request)
        with db.read() as s:
            lot = get(s, ProductLot, key)
            return render(request, "product.html", db_session=s, user=user, lot=lot, current=portion_view(s, get(s, Portion, lot.portion_id)),
                          spec=get(s, Version, lot.specification_id), packs=list(s.scalars(select(Pack).filter_by(lot_id=key).order_by(Pack.ordinal))))

    @app.get("/scan")
    def scan(request: Request, code: str):
        user_for(request)
        key = urlparse(code.strip()).path.rstrip("/").split("/")[-1]
        with db.read() as s:
            if s.get(Container, key):
                return redirect("/containers/" + key)
            if deleted_container(s, key):
                return redirect("/containers/" + key)
            if s.get(Location, key) or deleted_location(s, key):
                return redirect("/locations/" + key)
            if s.get(Portion, key):
                return redirect("/portions/" + key)
            if s.get(ProductLot, key):
                return redirect("/products/" + key)
            from .models import SortingCollection
            if s.get(SortingCollection,key):return redirect("/collections/"+key)
            matches = list(s.scalars(select(Container).filter_by(label=code.strip())))
            if matches:
                return redirect("/containers/" + matches[0].id)
        raise DomainError("No container or contents identity matches this scan")

    def label_target(s, kind, key):
        if kind == "collection":
            from .models import SortingCollection
            item=get(s,SortingCollection,key)
            return item.stream.replace("_"," ").title()+f" collection {item.ordinal} · "+get(s,Container,item.container_id).label,"/collections/"+key
        if kind == "container":
            item = get(s, Container, key)
            return item.label, "/containers/" + key
        if kind == "portion":
            get(s, Portion, key)
            return "Material contents", "/portions/" + key
        if kind == "product":
            lot = get(s, ProductLot, key)
            return lot.sku + " · product lot", "/products/" + key
        raise DomainError("Unknown label type")

    @app.get("/labels/{kind}/{key}", response_class=HTMLResponse)
    def label(request: Request, kind: str, key: str):
        user = user_for(request)
        with db.read() as s:
            title, path = label_target(s, kind, key)
            return render(request, "label.html", db_session=s, user=user, kind=kind, key=key, title=title, url=settings.public_url + path)

    @app.get("/qr/{kind}/{key}")
    def qr(request: Request, kind: str, key: str):
        user_for(request)
        with db.read() as s:
            _, path = label_target(s, kind, key)
        qr_image = qrcode.make(settings.public_url + path, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = BytesIO()
        qr_image.save(buffer)
        return Response(buffer.getvalue(), media_type="image/svg+xml")

    @app.get("/evidence", response_class=HTMLResponse)
    def evidence_lab(request: Request):
        user = user_for(request)
        # A teaching preview only. Does not attach synthetic evidence to real stock.
        profile = expected_profile([{"payload": fixture_particle(i)} for i in range(10000)], CAPTURE_DEFAULT,
                                   synthetic=True, coverage="complete synthetic fixture")
        return render(request, "evidence.html", user=user, profile=profile)

    from .sorter_controls import register_routes
    register_routes(app, db, settings, user_for, render, form)
    from .machine_jobs import register_routes as machine_routes
    machine_routes(app, db, machine, user_for, form)
    from .measurement_charts import register_routes as measurement_routes
    measurement_routes(app, db, user_for, render)
    from .machine_photos import register_routes as photo_routes
    photo_routes(app, db, machine, user_for)
    return app
