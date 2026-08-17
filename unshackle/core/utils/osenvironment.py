import platform


def get_os_arch(name: str) -> str:
    """Builds a name-os-arch based on the input name, system, architecture."""
    os_name = platform.system().lower()
    os_arch = platform.machine().lower()

    if os_name == "windows":
        os_name = "win"
    elif os_name == "darwin":
        os_name = "osx"
    else:
        os_name = "linux"

    if os_arch in ["x86_64", "amd64"]:
        os_arch = "x64"
    elif os_arch == "arm64":
        os_arch = "arm64"

    return f"{name}-{os_name}-{os_arch}"
