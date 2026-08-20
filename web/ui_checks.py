import re, sys

html = open(sys.argv[1], encoding="utf-8").read()
script = open(sys.argv[2], encoding="utf-8").read()
bad = 0

# --- elements the script reaches for must exist in the markup
ids = set(re.findall(r'id="([^"]+)"', html))
used = set(re.findall(r'\$\("([^"]+)"\)', script))
used |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
made = set(re.findall(r'\.id\s*=\s*"([^"]+)"', script))          # built at runtime
guarded = set(re.findall(r'getElementById\("([^"]+)"\)\?\.', script))
guarded |= set(re.findall(r'\$\("([^"]+)"\)\?\.', script))
missing = sorted(used - ids - made - guarded)
if missing:
    print("  elements: MISSING " + ", ".join("#" + m for m in missing)); bad = 1
else:
    print(f"  elements: ok ({len(used)} referenced, all present)")

# --- short identifiers used as objects must be declared somewhere
declared = set(re.findall(r'(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)', script))
declared |= set(re.findall(r'function\s*\(([^)]*)\)', script)[0].split(",")) if False else set()
# parameters and catch bindings count as declarations
for params in re.findall(r'\(([^()]*)\)\s*(?:=>|\{)', script):
    for p in params.split(","):
        p = p.strip().split("=")[0].strip()
        if re.fullmatch(r'[A-Za-z_$][\w$]*', p or ""):
            declared.add(p)
for p in re.findall(r'catch\s*\(\s*([A-Za-z_$][\w$]*)', script):
    declared.add(p)
for p in re.findall(r'for\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)', script):
    declared.add(p)
for a, b in re.findall(r'\[\s*([A-Za-z_$][\w$]*)\s*,\s*([A-Za-z_$][\w$]*)\s*\]\s*(?:of|=)', script):
    declared.add(a); declared.add(b)

BUILTINS = {
    "window", "document", "console", "Math", "JSON", "Object", "Array", "String",
    "Number", "Boolean", "Date", "Promise", "Set", "Map", "navigator", "location",
    "localStorage", "history", "performance", "URL", "Intl", "RegExp", "Error",
    "globalThis", "self", "crypto", "WebSocket", "FormData", "Blob", "File",
}
suspects = set()
for name in re.findall(r'\b([a-z][\w$]{0,3})\.[A-Za-z_$]', script):
    if name not in declared and name not in BUILTINS:
        suspects.add(name)
if suspects:
    print("  scope:    SUSPECT " + ", ".join(sorted(suspects))
          + "  (used as an object, never declared)")
    bad = 1
else:
    print("  scope:    ok")

sys.exit(bad)
