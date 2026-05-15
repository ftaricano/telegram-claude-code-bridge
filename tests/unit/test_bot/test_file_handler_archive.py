import io
import tarfile
import zipfile
from pathlib import Path

from src.bot.features.file_handler import FileHandler
from src.config.settings import Settings
from src.security.validators import SecurityValidator


def _handler(tmp_path: Path) -> FileHandler:
    settings = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=tmp_path,
    )
    return FileHandler(settings, SecurityValidator(tmp_path))


async def test_tar_archive_skips_path_traversal_and_links(tmp_path):
    handler = _handler(tmp_path)
    handler.temp_dir = tmp_path / "tmp"
    handler.temp_dir.mkdir()
    archive_path = tmp_path / "payload.tar"
    outside_path = tmp_path / "outside.txt"

    with tarfile.open(archive_path, "w") as archive:
        safe_bytes = b"print('ok')\n"
        safe_info = tarfile.TarInfo("src/app.py")
        safe_info.size = len(safe_bytes)
        archive.addfile(safe_info, io.BytesIO(safe_bytes))

        traversal_bytes = b"leak"
        traversal_info = tarfile.TarInfo("../outside.txt")
        traversal_info.size = len(traversal_bytes)
        archive.addfile(traversal_info, io.BytesIO(traversal_bytes))

        symlink_info = tarfile.TarInfo("src/link.py")
        symlink_info.type = tarfile.SYMTYPE
        symlink_info.linkname = str(outside_path)
        archive.addfile(symlink_info)

    result = await handler._process_archive(archive_path, "Review this")

    assert "src/app.py" in result.prompt
    assert "outside.txt" not in result.prompt
    assert not outside_path.exists()


async def test_zip_archive_skips_path_traversal(tmp_path):
    handler = _handler(tmp_path)
    handler.temp_dir = tmp_path / "tmp"
    handler.temp_dir.mkdir()
    archive_path = tmp_path / "payload.zip"
    outside_path = tmp_path / "outside.txt"

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/app.py", "print('ok')\n")
        archive.writestr("../outside.txt", "leak")

    result = await handler._process_archive(archive_path, "Review this")

    assert "src/app.py" in result.prompt
    assert "outside.txt" not in result.prompt
    assert not outside_path.exists()
