import subprocess
from typing import Dict

from modules.logging import Log
from modules.system import check_cmd
from modules.metadata import build_sdk_strings


def create_github_release(config_name: str, update_data: Dict) -> bool:
    if not check_cmd("gh"):
        Log.e("GitHub CLI (gh) not found. Cannot create release.")
        return False

    title = update_data.get("title")
    device = update_data.get("device")
    description = update_data.get("description")
    url = update_data.get("url")
    size = update_data.get("size")

    if not title or title == "Unknown Update":
        Log.w(f"Skipping GitHub release for {config_name}: Title is missing or unknown")
        return False

    if not device or device == "Unknown Device":
        Log.w(f"Skipping GitHub release for {config_name}: Device is missing or unknown")
        return False

    if not url:
        Log.w(f"Skipping GitHub release for {config_name}: Download URL is missing")
        return False

    description = description or "No description available"
    size = size or "Unknown size"
    fingerprint = update_data.get("fingerprint", "Unknown fingerprint")
    post_build_incremental = update_data.get("post_build_incremental")
    post_security_patch_level = update_data.get("post_security_patch_level")
    build_date = update_data.get("build_date")
    post_sdk_level = update_data.get("post_sdk_level")
    android_version = update_data.get("android_version")

    extra_lines = []
    if post_build_incremental:
        extra_lines.append(f"**Incremental:** {post_build_incremental}")
    if post_security_patch_level:
        extra_lines.append(f"**Security patch:** {post_security_patch_level}")
    if build_date:
        extra_lines.append(f"**Build date:** {build_date} (CST)")
    if post_sdk_level:
        _, _, release_line = build_sdk_strings(post_sdk_level, android_version)
        if release_line:
            extra_lines.append(release_line)

    extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    release_notes = f"""# {device}
## Changelog:
{description}

**Size:** {size}
**Download URL:** {url}{extra_block}
**Fingerprint:** `{fingerprint}`
"""

    try:
        check_release_cmd = ["gh", "release", "view", title]
        result = subprocess.run(check_release_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            Log.i(f"Release '{title}' for config {config_name} already exists. Skipping.")
            return True

        create_cmd = ["gh", "release", "create", title, "--title", title, "--notes", release_notes]

        result = subprocess.run(create_cmd, capture_output=True, text=True)

        if result.returncode == 0:
            Log.s(f"Created new release: {title} for config {config_name}")
            return True

        Log.e(f"Failed to create release: {result.stderr}")
        return False

    except Exception as exc:
        Log.e(f"Error creating GitHub release: {exc}")
        return False
