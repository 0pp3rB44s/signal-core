from flask import Blueprint

from .data_service import get_overview

api = Blueprint("api", __name__, url_prefix="/api")


@api.route("/health")
def health():
    return {
        "status": "online",
        "dashboard": "v3",
    }


@api.route("/overview")
def overview():
    return get_overview()
