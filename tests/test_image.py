import pytest

from qbridge import image


def _image_dir(tmp_path):
    (tmp_path / "local" / "snippets").mkdir(parents=True)
    return tmp_path


def test_install_copies_binary_and_snippets(tmp_path):
    img = _image_dir(tmp_path / "img")
    binary = tmp_path / "qbridged"
    binary.write_bytes(b"\x7fELF fake")
    out = image.install_qbridged(img, binary)
    assert (img / "local" / "qbridge" / "qbridged").read_bytes() == b"\x7fELF fake"
    sys_snip = (img / "local" / "snippets" / "system_files.qbridge").read_text()
    post_snip = (img / "local" / "snippets" / "post_start.qbridge").read_text()
    assert "bin/qbridged=local/qbridge/qbridged" in sys_snip
    assert "/system/bin/qbridged" in post_snip
    assert len(out["copied"]) == 3


def test_snippet_source_matches_install_location():
    # The system_files snippet must point at where install_qbridged copies the binary.
    text = (image.SNIPPETS / "system_files.qbridge").read_text()
    assert "=local/qbridge/qbridged" in text


def test_rejects_folder_that_is_not_an_image(tmp_path):
    with pytest.raises(FileNotFoundError, match="local/snippets"):
        image.install_qbridged(tmp_path, tmp_path / "x")


def test_missing_binary(tmp_path):
    with pytest.raises(FileNotFoundError, match="build it first"):
        image.install_qbridged(_image_dir(tmp_path), tmp_path / "nope")
