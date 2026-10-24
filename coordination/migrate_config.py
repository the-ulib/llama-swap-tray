"""Remove only the old ComfyUI process entry without reformatting user YAML."""
from pathlib import Path
import re
import sys

import yaml


def migrate(text, model_ids=()):
    config = yaml.safe_load(text)
    result = text
    removed = set(model_ids) & set(config.get("models", {}))
    if not removed:
        return text
    for model_id in removed:
        key = re.escape(model_id)
        spelling = '(?:' + key + '|"' + key + '"|\'' + key + '\')'
        result = re.sub(r"(?ms)^  " + spelling + r":[^\n]*\n.*?(?=^  \S[^\n]*:|^\S|\Z)", "", result, count=1)
        result = re.sub(r'(?m)^\s+- ' + spelling + r'\s*\n', "", result)
    parsed = yaml.safe_load(result)
    expected = yaml.safe_load(text)
    for model_id in removed:
        del expected["models"][model_id]
    for group in expected.get("groups", {}).values():
        group["members"] = [name for name in group.get("members", []) if name not in removed]
    if parsed != expected:
        raise ValueError("Unsupported YAML layout; migration would change unrelated settings")
    return result


if __name__ == "__main__":
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8-sig")
    path.write_text(migrate(text, sys.argv[2:]), encoding="utf-8")
