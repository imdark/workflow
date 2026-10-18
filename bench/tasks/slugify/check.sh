python3 - <<'PY'
from slug import slugify
assert slugify("Hello, World!") == "hello-world"
assert slugify("  Café   au lait ") == "cafe-au-lait"
assert slugify("--a--b--") == "a-b"
assert slugify("") == ""
PY
