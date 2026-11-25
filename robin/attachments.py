"""Anexos de texto: limites e montagem do contexto enviado ao provedor."""

import json
from pathlib import PurePosixPath
import unicodedata

ATTACHMENT_MARKER = "\n\nArquivos anexados como dados de referência (JSON):\n"

MAX_FILES = 4
MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 64 * 1024
EXTENSIONS = (
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
    ".html", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".ps1", ".sh", ".bat", ".cmd", ".sql",
    ".java", ".cs", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".swift", ".kt", ".r", ".lua",
)
BASENAMES = ("readme", "license", "dockerfile", "makefile", ".gitignore", ".gitattributes", ".editorconfig")


def attachment_limits() -> dict:
    return {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES,
            "extensions": EXTENSIONS, "basenames": BASENAMES}


def text_attachments(value) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError(f"Selecione até {MAX_FILES} anexos de texto por mensagem.")
    total = 0
    files = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content"), str):
            raise ValueError("Não foi possível ler um dos anexos.")
        name, content = item["name"], item["content"]
        if (not name or len(name) > 255 or any(char in name for char in "/\\:")
                or any(unicodedata.category(char).startswith("C") for char in name)):
            raise ValueError("Um dos nomes de arquivo é inválido.")
        if name.lower() not in BASENAMES and PurePosixPath(name).suffix.lower() not in EXTENSIONS:
            raise ValueError("Selecione arquivos de texto, código ou logs.")
        if any(ord(char) < 32 and char not in "\r\n\t" for char in content):
            raise ValueError("Um dos anexos contém dados binários.")
        try:
            count = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("Use arquivos de texto codificados em UTF-8.") from None
        if count > MAX_FILE_BYTES:
            raise ValueError("Cada anexo pode ter até 32 KB.")
        total += count
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Os anexos podem somar até 64 KB por mensagem.")
        files.append({"name": name, "content": content})
    return files


def message_with_attachments(text: str, files: list[dict[str, str]]) -> str:
    if not files:
        return text
    return text + ATTACHMENT_MARKER + json.dumps(files, ensure_ascii=False)
