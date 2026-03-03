# -*- coding:utf-8 -*-
# SPDX-FileCopyrightText: 2025-2026 SiFli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Optional
from typing import Tuple

from sdk_py_actions.cli.context import SdkContext
from sdk_py_actions.cli.registry import CommandRegistry
from sdk_py_actions.errors import AuthError
from sdk_py_actions.errors import CommandExecutionError
from sdk_py_actions.errors import UsageError
from sdk_py_actions.sf_pkg_auth import SfPkgAuthError
from sdk_py_actions.sf_pkg_auth import clear_users
from sdk_py_actions.sf_pkg_auth import delete_user
from sdk_py_actions.sf_pkg_auth import get_active_user
from sdk_py_actions.sf_pkg_auth import list_users
from sdk_py_actions.sf_pkg_auth import normalize_user
from sdk_py_actions.sf_pkg_auth import resolve_credentials
from sdk_py_actions.sf_pkg_auth import set_active_user
from sdk_py_actions.sf_pkg_auth import upsert_user
from sdk_py_actions.tools import print_warning

EXTENSION_ID = "sf-pkg"
EXTENSION_VERSION = "2.0.0"
EXTENSION_API_VERSION = 2
MIN_SDK_VERSION = None


def _extract_namespace(package_ref: str) -> Optional[str]:
    if "@" not in package_ref:
        return None

    namespace = package_ref.rsplit("@", 1)[1].strip()
    if not namespace:
        return None

    namespace = namespace.split("/", 1)[0].strip()
    if not namespace:
        return None

    return normalize_user(namespace)


def _group_user(sdk_ctx: SdkContext) -> Optional[str]:
    group_options = sdk_ctx.config.group_options.get("sf-pkg", {})
    group_user = group_options.get("sf_pkg_user")
    if not isinstance(group_user, str):
        return None

    group_user = group_user.strip()
    return group_user if group_user else None


def _to_auth_error(exc: Exception) -> AuthError:
    return AuthError(str(exc))


def _resolve_login_user(sdk_ctx: SdkContext, action_user: Optional[str]) -> str:
    global_user = _group_user(sdk_ctx)

    if action_user and global_user and normalize_user(action_user) != normalize_user(global_user):
        raise UsageError(
            'Conflicting users provided. Please use either "sf-pkg --user ... login" '
            'or "sf-pkg login --user ...", or keep them identical.'
        )

    selected_user = action_user or global_user
    if not selected_user:
        raise UsageError(
            'Missing user for login. Use "sdk.py sf-pkg --user <user> login -t <token>" '
            'or "sdk.py sf-pkg login -u <user> -t <token>".'
        )

    return normalize_user(selected_user)


def _login_remote(sdk_ctx: SdkContext, user: str, token: str) -> None:
    try:
        sdk_ctx.runner.run(["conan", "remote", "login", "-p", token, "artifactory", user], cwd=sdk_ctx.project_dir)
    except CommandExecutionError as exc:
        raise AuthError(f"Failed to login artifactory as {user}") from exc


def _resolve_auth_credentials(sdk_ctx: SdkContext, required: bool) -> Optional[Tuple[str, str]]:
    try:
        return resolve_credentials(_group_user(sdk_ctx), required=required)
    except SfPkgAuthError as exc:
        raise _to_auth_error(exc) from exc


def _ensure_remote_login(sdk_ctx: SdkContext, required: bool) -> Optional[Tuple[str, str]]:
    credentials = _resolve_auth_credentials(sdk_ctx, required=required)
    if credentials is None:
        return None

    user, token = credentials
    _login_remote(sdk_ctx, user, token)
    return user, token


def init_callback(sdk_ctx: SdkContext) -> None:
    result = sdk_ctx.runner.run(["conan", "new", "sf-pkg-project"], cwd=sdk_ctx.project_dir, check=False)
    if result.returncode != 0:
        raise CommandExecutionError("Failed to create dependency file")
    print("You can now add dependent packages in conanfile.txt")


def install_callback(sdk_ctx: SdkContext) -> None:
    _ensure_remote_login(sdk_ctx, required=False)
    sdk_ctx.runner.run(
        [
            "conan",
            "install",
            ".",
            "--output-folder=sf-pkgs",
            "--deployer=full_deploy",
            "--envs-generation=false",
            "-r=artifactory",
        ],
        cwd=sdk_ctx.project_dir,
    )
    print("Packages installed successfully")


def new_callback(
    sdk_ctx: SdkContext,
    name: str = "mypackage",
    version: Optional[str] = None,
    license: Optional[str] = None,
    author: Optional[str] = None,
    support_sdk_version: Optional[str] = None,
) -> None:
    credentials = _ensure_remote_login(sdk_ctx, required=True)
    if not credentials:
        raise AuthError("No available user credentials. Please login first.")

    user, _token = credentials
    cmd = [
        "conan",
        "new",
        "sf-pkg-package",
        "-d",
        f"name={name}",
        "-d",
        f"user={user}",
    ]

    if version:
        cmd.extend(["-d", f"version={version}"])
    if license:
        cmd.extend(["-d", f"license={license}"])
    if author:
        cmd.extend(["-d", f"author={author}"])
    if support_sdk_version:
        cmd.extend(["-d", f"support_sdk_version={support_sdk_version}"])

    sdk_ctx.runner.run(cmd, cwd=sdk_ctx.project_dir)
    print(f"Created new package: {name}")


def build_callback(sdk_ctx: SdkContext, version: str) -> None:
    _ensure_remote_login(sdk_ctx, required=False)
    sdk_ctx.runner.run(["conan", "create", "--version", version, "."], cwd=sdk_ctx.project_dir)
    print(f"Package built successfully with version: {version}")


def search_callback(sdk_ctx: SdkContext, name: str) -> None:
    _ensure_remote_login(sdk_ctx, required=False)
    search_pattern = name if "*" in name else f"{name}/*"
    sdk_ctx.runner.run(["conan", "search", search_pattern, "-r=artifactory"], cwd=sdk_ctx.project_dir)
    print(f"Search completed for: {search_pattern}")


def upload_callback(sdk_ctx: SdkContext, name: str, keep: bool = False) -> None:
    credentials = _resolve_auth_credentials(sdk_ctx, required=True)
    if not credentials:
        raise AuthError("No available user credentials. Please login first.")

    user, token = credentials
    package_user = _extract_namespace(name)
    if package_user and package_user != user:
        raise UsageError(
            f'Package namespace "{package_user}" does not match selected user "{user}". '
            "Use matching --user or update --name."
        )

    _login_remote(sdk_ctx, user, token)
    sdk_ctx.runner.run(["conan", "upload", name, "-r=artifactory"], cwd=sdk_ctx.project_dir)
    print(f"Package {name} uploaded successfully")

    if not keep:
        sdk_ctx.runner.run(["conan", "remove", name, "-c"], cwd=sdk_ctx.project_dir, check=False)
        print(f"Package {name} removed from local cache")

    try:
        import requests

        sync_url = "https://packages.sifli.com/api/v1/sync"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        print("Syncing package to public repository...")
        response = requests.post(sync_url, json={}, headers=headers, timeout=30)

        if response.status_code == 200:
            print("Package synced to public repository successfully")
        else:
            print_warning(f"WARNING: Sync failed with status code {response.status_code}")
            print_warning(f"WARNING: Response: {response.text}")

    except ImportError:
        print_warning("WARNING: requests library not available. Skipping sync to public repository.")
    except Exception as exc:
        print_warning(f"WARNING: Failed to sync to public repository: {exc}")


def remove_callback(sdk_ctx: SdkContext, name: str, remote: bool = False) -> None:
    if remote:
        _ensure_remote_login(sdk_ctx, required=True)
        sdk_ctx.runner.run(["conan", "remove", name, "-r=artifactory", "-c"], cwd=sdk_ctx.project_dir)
        print(f"Package {name} removed from remote repository: artifactory")
        return

    sdk_ctx.runner.run(["conan", "remove", name, "-c"], cwd=sdk_ctx.project_dir)
    print(f"Package {name} removed from local cache")


def login_callback(sdk_ctx: SdkContext, token: str, user: Optional[str] = None) -> None:
    selected_user = _resolve_login_user(sdk_ctx, user)
    _login_remote(sdk_ctx, selected_user, token)

    try:
        stored_user = upsert_user(selected_user, token)
    except SfPkgAuthError as exc:
        raise _to_auth_error(exc) from exc

    print("Logged in to SiFli package registry")
    print(f"Credentials stored for user: {stored_user}")
    print(f"Active user set to: {stored_user}")


def logout_callback(sdk_ctx: SdkContext, name: Optional[str] = None) -> None:
    target_user = name or _group_user(sdk_ctx)

    if target_user:
        selected_user = normalize_user(target_user)
        try:
            removed = delete_user(selected_user)
        except SfPkgAuthError as exc:
            raise _to_auth_error(exc) from exc

        if removed:
            sdk_ctx.runner.run(["conan", "remote", "logout", "artifactory"], cwd=sdk_ctx.project_dir, check=False)
            print(f"Logged out user: {selected_user}")
            current = get_active_user()
            if current:
                print(f"Active user switched to: {current}")
            return

        print(f"User {selected_user} not found in stored credentials")
        return

    sdk_ctx.runner.run(["conan", "remote", "logout", "artifactory"], cwd=sdk_ctx.project_dir, check=False)
    clear_users()
    print("Logged out all users and cleared credentials")


def users_callback(sdk_ctx: SdkContext) -> None:
    users = list_users()
    active = get_active_user()
    if not users:
        print('No stored sf-pkg users. Please login using "sdk.py sf-pkg login".')
        return

    print("Stored sf-pkg users:")
    for user in users:
        marker = "*" if user == active else " "
        print(f" {marker} {user}")
    if active:
        print(f"Active user: {active}")


def use_callback(sdk_ctx: SdkContext, name: str) -> None:
    try:
        selected_user = set_active_user(name)
    except SfPkgAuthError as exc:
        raise _to_auth_error(exc) from exc

    print(f"Active sf-pkg user set to: {selected_user}")


def current_user_callback(sdk_ctx: SdkContext) -> None:
    active = get_active_user()
    if not active:
        print('No active sf-pkg user. Use "sdk.py sf-pkg login" or "sdk.py sf-pkg use" first.')
        return

    print(active)


def register(registry: CommandRegistry) -> None:
    registry.group(
        path="sf-pkg",
        help="SiFli package management.",
        options=[
            {
                "names": ["--user", "-u", "sf_pkg_user"],
                "help": "Select sf-pkg user for this group.",
                "default": None,
            }
        ],
    )

    registry.command(
        path="sf-pkg/login",
        callback=login_callback,
        help="Login to SiFli package registry and store credentials.",
        options=[
            {
                "names": ["--user", "-u"],
                "help": "Username for login. If omitted, group --user is used.",
                "default": None,
            },
            {
                "names": ["--token", "-t"],
                "help": "API token for login.",
                "required": True,
            },
        ],
    )

    registry.command(
        path="sf-pkg/logout",
        callback=logout_callback,
        help="Logout from SiFli package registry and clear credentials.",
        options=[
            {
                "names": ["--name", "-n"],
                "help": "Username to logout. If omitted, uses group --user or logs out all users.",
                "default": None,
            }
        ],
    )

    registry.command(path="sf-pkg/users", callback=users_callback, help="List local sf-pkg users and active user.")

    registry.command(
        path="sf-pkg/use",
        callback=use_callback,
        help="Switch active sf-pkg user.",
        options=[
            {
                "names": ["--name", "-n"],
                "help": "Username to set as active user.",
                "required": True,
            }
        ],
    )

    registry.command(
        path="sf-pkg/current-user",
        callback=current_user_callback,
        help="Show current active sf-pkg user.",
    )

    registry.command(path="sf-pkg/init", callback=init_callback, help="Initialize project dependencies.")
    registry.command(path="sf-pkg/install", callback=install_callback, help="Install SiFli-SDK packages.")

    registry.command(
        path="sf-pkg/new",
        callback=new_callback,
        help="Create a new SiFli-SDK package.",
        options=[
            {"names": ["--name", "-n"], "help": "Package name.", "default": "mypackage", "required": True},
            {"names": ["--version"], "help": "Package version.", "default": None},
            {"names": ["--license"], "help": "Package license.", "default": None},
            {"names": ["--author"], "help": "Package author.", "default": None},
            {
                "names": ["--support-sdk-version"],
                "help": "Supported SiFli-SDK version.",
                "default": None,
            },
        ],
    )

    registry.command(
        path="sf-pkg/build",
        callback=build_callback,
        help="Build the package for upload.",
        options=[
            {
                "names": ["--version", "-v"],
                "help": "Version to be built.",
                "required": True,
            }
        ],
    )

    registry.command(
        path="sf-pkg/upload",
        callback=upload_callback,
        help="Upload the specified package.",
        options=[
            {
                "names": ["--name", "-n"],
                "help": "Name of package to be uploaded.",
                "required": True,
            },
            {
                "names": ["--keep"],
                "help": "Keep local cache after upload.",
                "is_flag": True,
                "default": False,
            },
        ],
    )

    registry.command(
        path="sf-pkg/remove",
        callback=remove_callback,
        help="Remove package from local cache or from remote artifactory.",
        options=[
            {
                "names": ["--name"],
                "help": "Name of package to be removed.",
                "required": True,
            },
            {
                "names": ["--remote"],
                "help": "Remove package from remote artifactory instead of local cache.",
                "is_flag": True,
                "default": False,
            },
        ],
    )

    registry.command(
        path="sf-pkg/search",
        callback=search_callback,
        help="Search for packages in SiFli package registry.",
        arguments=[
            {
                "names": ["name"],
                "required": True,
            }
        ],
    )
