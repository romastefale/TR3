def detect_intent(text: str) -> str | None:
    if not text:
        return None

    value = text.strip().lower()
    aliases = {
        "tocando", "pifm", "cyo", "py", "braya", "dead", "ag", "rosan",
        "roro", "ro", "rafarl", "pipi", "bressing", "kur", "xxt", "ts",
        "cebrutius", "tigraofm", "djpi", "royalfm", "geeksfm", "radinho", "qap",
    }
    if value in aliases:
        return "play"
    return None
