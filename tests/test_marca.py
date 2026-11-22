"""Marca do Robin: ícone do exe e da janela, AppUserModelID e o R da interface igual ao do brand."""

import re
import struct
from pathlib import Path

from robin import config

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "ui" / "assets" / "brand"
INDEX = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def _ico_sizes(path: Path) -> list[int]:
    data = path.read_bytes()
    count = struct.unpack("<HHH", data[:6])[2]
    return [data[6 + 16 * i] or 256 for i in range(count)]


def _shapes(svg: str) -> tuple[str, str]:
    """Pontos do traço (polygon) e o caminho do corpo (path), sem espaços sobrando."""
    points = re.search(r'<polygon[^>]*points="([^"]+)"', svg).group(1)
    path = re.search(r'<path[^>]*\sd="([^"]+)"', svg).group(1)
    numbers = lambda text: re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", text)  # noqa: E731
    return " ".join(numbers(points)), " ".join(numbers(path))


def test_icone_tem_de_16_a_256():
    assert config.ICON_FILE == BRAND / "robin.ico"
    sizes = _ico_sizes(config.ICON_FILE)
    assert min(sizes) == 16 and max(sizes) == 256


def test_exe_e_janela_usam_o_mesmo_icone():
    spec = (ROOT / "scripts" / "robin.spec").read_text(encoding="utf-8")
    assert 'icon=os.path.join(ROOT, "ui", "assets", "brand", "robin.ico")' in spec
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "icon=str(config.ICON_FILE)" in app
    assert "set_app_user_model_id(config.APP_USER_MODEL_ID)" in app
    assert config.APP_USER_MODEL_ID == "mxndex7.Robin"


def test_o_r_da_interface_e_o_do_brand():
    expected = _shapes((BRAND / "robin-symbol.svg").read_text(encoding="utf-8"))
    symbol = re.search(r'<symbol id="i-robin".*?</symbol>', INDEX, re.S).group(0)
    empty = re.search(r'<svg class="mark empty__mark".*?</svg>', INDEX, re.S).group(0)
    assert _shapes(symbol) == expected
    assert _shapes(empty) == expected
    # Centro visual compensado: recorte de 176 centrado em y = 128 + 7,5.
    for svg in (symbol, empty):
        assert 'viewBox="40 47.5 176 176"' in svg


def test_favicon_e_sem_cor_fixa_na_marca():
    assert '<link rel="icon" href="assets/brand/robin.ico"' in INDEX
    symbol = re.search(r'<symbol id="i-robin".*?</symbol>', INDEX, re.S).group(0)
    assert "fill=" not in symbol and "#" not in symbol  # a cor vem de currentColor (.mark)
