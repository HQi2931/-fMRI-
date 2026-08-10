"""ASGI module and console entry point."""

from neuroagent.api.app import create_app

app = create_app()


def main() -> None:
    import uvicorn

    settings = app.state.service.settings
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
