"""Small, bounded SQLite equivalents for generated portable expressions."""

import re


def register(connection):
    def replace_characters(source, pattern, replacement, *flags):
        if source is None:
            return None
        if pattern != r"[^0-9.\-]" or replacement != "" or flags not in ((), ("g",)):
            raise ValueError("SQLite supports only the numeric-text normalization pattern")
        return re.sub(pattern, replacement, str(source))

    connection.create_function("REGEXP_REPLACE", -1, replace_characters, deterministic=True)
