"""Configurable assumptions, independent of measurements and machine controls."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func, select

from .auth import require_role
from .capture import CAPTURE_DEFAULT, probability
from .models import SimulationAssumption, SimulationSettings, User, Version, identity
from .quantities import DomainError

MAX_SIMULATION_PARTICLES = 1_000_000
DEFAULT_MEAN_MASS_G = "0.15"
DEFAULT_CAPTURE_PERCENT = "99"
RESEARCH = [
    {"title": "Cassamo et al. (2022), Agronomy, Table 3",
     "url": "https://www.mdpi.com/2073-4395/12/10/2540",
     "finding": "Green Arabica treatment means: 13.2–18.5 g per 100 beans (0.132–0.185 g per bean); samples at approximately 11–12% moisture."},
    {"title": "Coffee bean particle motion measured using PEPT (2023), Table 1",
     "url": "https://www.sciencedirect.com/science/article/pii/S0963996922013114",
     "finding": "Kenyan green Arabica: 7,420 beans/kg, equivalent to approximately 0.135 g per bean."},
]
RESEARCH_NOTE = ("0.15 g is a rounded engineering assumption informed by these samples, not a universal "
                 "or market-weighted mean. Variety, grade, moisture and defects affect mass. Weigh a counted "
                 "representative sample to choose a value for your lot. This is not a moisture measurement.")


def decimal_setting(value, label):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise DomainError(f"{label} must be entered as a decimal value")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise DomainError(f"Invalid {label}") from None
    if not result.is_finite():
        raise DomainError(f"Invalid {label}")
    return result


def current_settings(session):
    return session.scalar(select(SimulationSettings).order_by(SimulationSettings.version.desc()).limit(1))


def mean_mass_g(settings):
    return format(Decimal(settings.mean_mass_ug) / 1_000_000, "f")


def estimate_particles(mass_mg, settings):
    """Decimal expectation; round half up only for an integer synthetic fixture."""
    if type(mass_mg) is not int or mass_mg <= 0:
        raise DomainError("Count estimation requires positive integer milligrams")
    count = Decimal(mass_mg) * 1000 / settings.mean_mass_ug
    return {"method": "mass-over-assumed-mean/v1", "settings_id": settings.id,
            "settings_version": settings.version, "material_label": settings.material_label,
            "mass_basis_mg": mass_mg, "mean_mass_ug": settings.mean_mass_ug,
            "mean_mass_g": mean_mass_g(settings), "estimated_particles": format(count, "f"),
            "rounded_particles": int(count.to_integral_value(rounding=ROUND_HALF_UP)),
            "rounding": "nearest integer, half up; only for synthetic run size / approximate display",
            "unit": "particles", "evidence": "calculated_prediction", "mean_mass_evidence": "assumption",
            "physical_census": False,
            "assumptions": ["The accepted material mean represents this input population.",
                            "Count estimation does not change measured mass or establish tracker coverage."]}


def pin_assumption(session, operation, settings, mass_mg, *, source_population=None):
    data = source_population["count_basis"] if source_population else estimate_particles(mass_mg, settings)
    session.add(SimulationAssumption(id=identity("assumption"), operation_id=operation.id,
                                    settings_id=settings.id, data=data))
    return data


class SimulationConfiguration:
    def __init__(self, db):
        self.db = db

    def save(self, user_id, key, data):
        # Local import keeps application commands separate from pure estimators.
        from .service import audit, get, remember, replay
        payload = {"command": "simulation_settings", "operator": user_id, **data}
        with self.db.write() as s:
            user = get(s, User, user_id)
            require_role(user, "admin")
            old = replay(s, key, payload)
            if old:
                return old
            previous = current_settings(s)
            if (previous.id if previous else None) != (data.get("previous_id") or None):
                raise DomainError("Simulation settings changed in another session; reload before saving")
            mean = decimal_setting(data.get("mean_mass_g"), "mean bean mass")
            if not Decimal("0.000001") <= mean <= 1000 or mean != mean.quantize(Decimal("0.000001")):
                raise DomainError("Mean bean mass must be 0.000001–1000 g with at most six decimal places")
            percent = decimal_setting(data.get("capture_percent"), "capture percentage")
            if not 0 <= percent <= 100 or percent != percent.quantize(Decimal("0.0001")):
                raise DomainError("Capture percentage must be 0–100 with at most four decimal places")
            label, reason = data.get("material_label", "").strip(), data.get("reason", "").strip()
            if not 1 <= len(label) <= 120 or not 1 <= len(reason) <= 2000:
                raise DomainError("Provide a material description (1–120 characters) and change reason (1–2000 characters)")
            number = (previous.version if previous else 0) + 1
            name = "Simulation capture"
            model = {**CAPTURE_DEFAULT, "probability": format(percent / 100, "f"),
                     "parameter_evidence": "assumption", "simulation_settings_version": number}
            probability(model)
            capture = Version(id=identity("version"), kind="capture_model", name=name,
                version=(s.scalar(select(func.max(Version.version)).filter_by(kind="capture_model", name=name)) or 0) + 1,
                data=model, operator_id=user.id)
            s.add(capture)
            s.flush()
            item = SimulationSettings(id=identity("simulation-settings"), version=number, material_label=label,
                mean_mass_ug=int(mean * 1_000_000), capture_model_id=capture.id, operator_id=user.id,
                reason=reason, provenance={"evidence": "assumption", "basis": "operator-accepted simulation assumption",
                    "initial_research": RESEARCH, "initial_research_note": RESEARCH_NOTE,
                    "note": "Later edits are operator choices; the literature does not validate edited values or capture rate."})
            s.add(item)
            audit(s, user, "simulation_settings_created", {"settings_id": item.id, "version": number,
                "mean_mass_ug": item.mean_mass_ug, "capture_percent": format(percent, "f"),
                "material_label": label, "reason": reason})
            remember(s, key, payload, item.id)
            return item.id

    def initialize(self, user_id):
        with self.db.read() as s:
            existing = current_settings(s)
            if existing:
                return existing.id
        return self.save(user_id, "initial-simulation-settings-v1", {
            "mean_mass_g": DEFAULT_MEAN_MASS_G, "capture_percent": DEFAULT_CAPTURE_PERCENT,
            "material_label": "Green coffee", "reason": "Research-informed initial bean mass; user-requested 99% capture assumption."})
