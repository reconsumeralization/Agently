from __future__ import annotations

from typing import Any


def restore_resume_service(context: dict[str, Any]):
    resource_key = context["resource_key"]

    def service(payload: dict[str, Any]):
        return {
            "approved": payload["approved"],
            "source": resource_key,
        }

    return {"resource": service, "health": "healthy"}


async def restore_resume_service_async(context: dict[str, Any]):
    return restore_resume_service(context)


def unhealthy_resume_service(context: dict[str, Any]):
    return {
        "health": "unhealthy",
        "message": f"{ context['resource_key'] } is not ready",
    }


def unavailable_resume_service(context: dict[str, Any]):
    raise RuntimeError(f"{ context['resource_key'] } unavailable")
