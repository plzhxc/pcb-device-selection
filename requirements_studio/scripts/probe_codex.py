"""Verify the fixed Codex route without reading project files or using tools."""
import json
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
folder = root / ".runtime" / "probe"
folder.mkdir(parents=True, exist_ok=True)
schema = folder / "schema.json"
schema.write_text(json.dumps({"type": "object", "properties": {"status": {"type": "string", "enum": ["ready"]}}, "required": ["status"], "additionalProperties": False}))
binary = shutil.which("codex")
if not binary:
    raise SystemExit("Codex CLI unavailable")
command = [binary, "exec", "--ephemeral", "--ignore-user-config", "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never", "--json", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="xhigh"', "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"', "-C", str(folder), "--output-schema", str(schema), "-o", str(folder / "answer.json"), "-"]
result = subprocess.run(command, input="Return exactly the JSON object required by the schema with status ready. Do not read files, use tools, or run commands.", text=True, capture_output=True, timeout=240)
(folder / "events.jsonl").write_text(result.stdout)
(folder / "stderr.log").write_text(result.stderr)
if result.returncode:
    print(json.dumps({"ok": False, "exit_code": result.returncode, "error": result.stderr[-1000:]}))
    raise SystemExit(1)
answer = json.loads((folder / "answer.json").read_text())
print(json.dumps({"ok": answer == {"status": "ready"}, "model": "gpt-6-luna", "effort": "xhigh", "answer": answer}))
