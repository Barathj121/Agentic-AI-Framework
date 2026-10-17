# External application demonstration

This small, trusted local HTTP application demonstrates the launcher.
It does not implement customer analytics. The image reuses the already built sandbox
base and needs no package downloads.

From the repository root, after building the sandbox image:

```powershell
docker build --pull=false -f examples/external_app/Dockerfile -t workbench-launcher-demo:latest .
```

In **Applications**, register the name `Customer Insights demo`, image
`workbench-launcher-demo:latest`, and internal port `8501`. Confirm that you trust
the image, register, launch, open its localhost URL, and stop it.

The launcher pins the local image ID at registration and never pulls images. A new
image version needs a separately named registration. Containers run without host
mounts or privileges, as UID/GID 10001, with a read-only filesystem and writable
`/tmp`, 2 CPUs and 2 GB of RAM. Image-declared volumes are unsupported. The Docker
default bridge is used, so trusted applications may make outbound network requests;
only their published inbound port is restricted to localhost. Docker's configured
proxy variables are cleared. The launcher does not pass host API keys or credentials.

Only compatible trusted Linux images are supported: the application must listen on
`0.0.0.0` at the registered internal port. No persistent application data is provided;
stopping removes the container. A running status means the process is running, not
that an application-specific readiness check has passed.
