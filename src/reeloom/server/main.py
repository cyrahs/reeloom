from __future__ import annotations

import uvicorn

from reeloom.server.auth import AuthSettings
from reeloom.server.composition import build_application
from reeloom.server.settings import DeploymentSettings


def serve() -> None:
    settings = DeploymentSettings.from_environ()
    auth = AuthSettings.from_environ()
    application = build_application(settings, auth=auth)
    try:
        uvicorn.run(
            application.api,
            host="0.0.0.0",
            port=8080,
            workers=1,
            access_log=False,
            server_header=False,
        )
    finally:
        application.close()


if __name__ == "__main__":
    serve()
