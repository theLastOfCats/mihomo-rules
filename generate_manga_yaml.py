#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "PyYAML>=6.0,<7",
# ]
# ///
"""Generate Mihomo manga YAML and MRS rule providers from Kotatsu parser sources.

The script writes its outputs beside itself and reads parser sources from an explicit
Kotatsu checkout. Run it from this rule repository:

    uv run generate_manga_yaml.py \
        --source-root /path/to/kotatsu-parsers-redo \
        --mihomo /path/to/mihomo

Use ``--output`` and ``--mrs-output`` to write alternate provider paths.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import yaml

RULES_ROOT = Path(__file__).resolve().parent
PARSER_SOURCE_RELATIVE_PATH = Path("src/main/kotlin/org/koitharu/kotatsu/parsers/site")
DEFAULT_OUTPUT = RULES_ROOT / "manga.yaml"
DEFAULT_MRS_OUTPUT = RULES_ROOT / "manga.mrs"

HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}(?:\.[a-z]{2,})?$",
)
FILE_EXTENSION_RE = re.compile(
    r"\.(?:php|html?|jpg|jpeg|png|gif|webp|css|js|json|svg|xml|txt|asp|aspx)$",
    re.IGNORECASE,
)
SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
STRING_LITERAL_RE = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]\w*\b")

STRING_CONSTANT_RE = re.compile(
    r"\b(?:private\s+)?(?:const\s+)?(?:val|var)\s+"
    r"(?P<name>\w+)\s*(?::\s*String)?\s*=\s*\"(?P<value>[^\"]+)\"",
)
COLLECTION_CONSTANT_RE = re.compile(
    r"\b(?:private\s+)?(?:const\s+)?(?:val|var)\s+"
    r"(?P<name>\w+)\s*=\s*(?:arrayOf|listOf|setOf)\s*\("
    r"(?P<values>(?:[^()]|\([^()]*\))*)\)",
    re.DOTALL,
)
CONFIG_KEY_DOMAIN_RE = re.compile(r"ConfigKey\.Domain\s*\(")
NAMED_DOMAIN_COLLECTION_RE = re.compile(
    r"\b(?:siteDomains|domains|domainArray|domainList)\s*=\s*"
    r"(?:arrayOf|listOf|setOf)\s*\("
    r"(?P<values>(?:[^()]|\([^()]*\))*)\)",
    re.DOTALL,
)
SOURCE_ARGUMENT_RE = re.compile(r"MangaParserSource\.\w+\s*,")
STRING_AT_START_RE = re.compile(r'\s*"(?P<value>[^\"]+)"')
COLLECTION_AT_START_RE = re.compile(r"\s*(?:arrayOf|listOf|setOf)\s*\(")
IDENTIFIER_AT_START_RE = re.compile(r"\s*(?P<name>[A-Za-z_]\w*)")

STRING_VARIABLE_RE = re.compile(
    r"\b(?:private\s+|protected\s+|internal\s+|public\s+)?"
    r"(?:const\s+)?(?:val|var)\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?::\s*[^\n=]+)?=\s*\"(?P<value>[^\"]+)\"",
)
HTTP_URL_LITERAL_RE = re.compile(
    r'"(?P<value>https?://[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/:?#][^\"]*)?)"',
    re.IGNORECASE,
)

# Public infrastructure belongs to unrelated applications too; do not route it all
# through the manga proxy. Narrow source-owned subdomains remain eligible.
GENERIC_INFRASTRUCTURE = frozenset(
    {
        "google.com",
        "googleapis.com",
        "gstatic.com",
        "ggpht.com",
        "googleusercontent.com",
        "googlevideo.com",
        "github.com",
        "githubusercontent.com",
        "github.io",
        "git.io",
        "wordpress.com",
        "wp.com",
        "weserv.nl",
        "statically.io",
        "b-cdn.net",
        "regru.cloud",
        "cloudfront.net",
        "jsdelivr.net",
        "cdnjs.net",
        "bootstrapcdn.com",
        "akamaihd.net",
        "akamaized.net",
    },
)
EXPLICIT_EXCLUSIONS = frozenset({"kotatsu.intercept", "sceneario.com"})
FUNCTIONAL_NAME_FRAGMENTS = ("api", "cdn", "image", "img", "static", "media", "asset", "base")


Source = tuple[Path, str]


def load_kotlin_sources(root: Path) -> list[Source]:
    """Read the parser tree once, in deterministic order."""
    return [
        (path, path.read_text(encoding="utf-8", errors="replace"))
        for path in sorted(root.rglob("*.kt"))
    ]


def string_literals(text: str) -> Iterator[str]:
    """Return plain Kotlin string literals used as host candidates."""
    for match in STRING_LITERAL_RE.finditer(text):
        yield match.group(1)


def parenthesized_content(text: str, opening_parenthesis: int) -> str:
    """Return the content of a balanced parenthesized Kotlin expression."""
    if opening_parenthesis >= len(text) or text[opening_parenthesis] != "(":
        raise ValueError("expected an opening parenthesis")

    depth = 0
    in_string = False
    escaped = False
    for index in range(opening_parenthesis, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[opening_parenthesis + 1 : index]

    raise ValueError("unbalanced parenthesized expression")


def collect_constants(sources: Iterable[Source]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Index string and collection constants so cross-file references resolve."""
    string_constants: dict[str, str] = {}
    collection_constants: dict[str, list[str]] = {}

    for _, text in sources:
        for match in STRING_CONSTANT_RE.finditer(text):
            string_constants.setdefault(match.group("name"), match.group("value"))
        for match in COLLECTION_CONSTANT_RE.finditer(text):
            collection_constants.setdefault(
                match.group("name"),
                list(string_literals(match.group("values"))),
            )

    return string_constants, collection_constants


def resolve_identifier(
    name: str,
    string_constants: dict[str, str],
    collection_constants: dict[str, list[str]],
) -> Iterable[str]:
    """Resolve a host-bearing constant or collection by name."""
    if name in collection_constants:
        return collection_constants[name]
    if name in string_constants:
        return (string_constants[name],)
    return ()


def config_key_domains(
    text: str,
    string_constants: dict[str, str],
    collection_constants: dict[str, list[str]],
) -> Iterator[str]:
    """Extract direct literals and constants passed to ConfigKey.Domain(...)."""
    for match in CONFIG_KEY_DOMAIN_RE.finditer(text):
        arguments = parenthesized_content(text, match.end() - 1)
        yield from string_literals(arguments)
        for identifier in IDENTIFIER_RE.findall(arguments):
            yield from resolve_identifier(identifier, string_constants, collection_constants)


def named_domain_collections(text: str) -> Iterator[str]:
    """Extract array arguments forwarded as siteDomains/domains by parser bases."""
    for match in NAMED_DOMAIN_COLLECTION_RE.finditer(text):
        yield from string_literals(match.group("values"))


def template_constructor_domains(
    text: str,
    string_constants: dict[str, str],
    collection_constants: dict[str, list[str]],
) -> Iterator[str]:
    """Extract a host passed immediately after a MangaParserSource enum value."""
    for match in SOURCE_ARGUMENT_RE.finditer(text):
        argument = text[match.end() :]

        literal = STRING_AT_START_RE.match(argument)
        if literal:
            yield literal.group("value")
            continue

        collection = COLLECTION_AT_START_RE.match(argument)
        if collection:
            values = parenthesized_content(argument, collection.end() - 1)
            yield from string_literals(values)
            continue

        identifier = IDENTIFIER_AT_START_RE.match(argument)
        if identifier:
            yield from resolve_identifier(
                identifier.group("name"),
                string_constants,
                collection_constants,
            )


def is_functional_host_variable(name: str) -> bool:
    """Recognize parser variables that identify an API, image, or CDN endpoint."""
    lower_name = name.lower()
    return any(fragment in lower_name for fragment in FUNCTIONAL_NAME_FRAGMENTS)


def functional_hosts(text: str) -> Iterator[str]:
    """Extract functional endpoints beyond the parser's primary site domain."""
    for match in STRING_VARIABLE_RE.finditer(text):
        if is_functional_host_variable(match.group("name")):
            yield match.group("value")
    for match in HTTP_URL_LITERAL_RE.finditer(text):
        yield match.group("value")


def normalize_host(value: str) -> str | None:
    """Normalize a host or URL literal and reject non-host values."""
    candidate = SCHEME_RE.sub("", value.strip().strip('"'))
    candidate = candidate.split("/", 1)[0]
    candidate = candidate.split("?", 1)[0]
    candidate = candidate.split("#", 1)[0]
    candidate = candidate.split(":", 1)[0].lower()

    if not candidate or " " in candidate:
        return None
    if FILE_EXTENSION_RE.search(candidate) or not HOST_RE.fullmatch(candidate):
        return None
    return candidate


def is_excluded(host: str) -> bool:
    """Return whether host belongs to broad generic infrastructure or explicit noise."""
    if host in EXPLICIT_EXCLUSIONS or host.endswith(".intercept"):
        return True
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in GENERIC_INFRASTRUCTURE
    )


def deduplicate_suffixes(hosts: Iterable[str]) -> list[str]:
    """Keep only the shortest suffix; it already covers each of its subdomains."""
    kept: set[str] = set()
    for host in sorted(set(hosts), key=lambda value: (value.count("."), value)):
        if not any(host.endswith("." + suffix) for suffix in kept):
            kept.add(host)
    return sorted(kept)


def collect_domain_suffixes(sources: Iterable[Source]) -> list[str]:
    """Collect, normalize, filter, and suffix-deduplicate all parser endpoints."""
    source_list = list(sources)
    string_constants, collection_constants = collect_constants(source_list)
    candidates: set[str] = set()

    for _, text in source_list:
        candidates.update(config_key_domains(text, string_constants, collection_constants))
        candidates.update(named_domain_collections(text))
        candidates.update(
            template_constructor_domains(text, string_constants, collection_constants),
        )
        candidates.update(functional_hosts(text))

    normalized: set[str] = set()
    for candidate in candidates:
        host = normalize_host(candidate)
        if host is None:
            continue
        host = host.removeprefix("www.")
        if not is_excluded(host):
            normalized.add(host)

    return deduplicate_suffixes(normalized)


def write_rule_provider(output: Path, domains: Iterable[str]) -> None:
    """Write the complete YAML provider from a regular Python mapping."""
    rule_provider = {"payload": [f"+.{domain}" for domain in domains]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(
            rule_provider,
            stream,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )

def write_mrs_rule_provider(mihomo: Path, yaml_output: Path, mrs_output: Path) -> None:
    """Convert a Mihomo YAML domain provider to its MRS representation."""
    mrs_output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(mihomo),
            "convert-ruleset",
            "domain",
            "yaml",
            str(yaml_output),
            str(mrs_output),
        ],
        check=True,
    )


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="root of the Kotatsu parser checkout",
    )
    parser.add_argument(
        "--mihomo",
        type=Path,
        required=True,
        help="Mihomo executable used to convert YAML to MRS",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="destination YAML file (default: manga.yaml beside this script)",
    )
    parser.add_argument(
        "--mrs-output",
        type=Path,
        default=DEFAULT_MRS_OUTPUT,
        help="destination MRS file (default: manga.mrs beside this script)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    source_root = arguments.source_root.expanduser().resolve()
    parser_root = source_root / PARSER_SOURCE_RELATIVE_PATH
    mihomo = arguments.mihomo.expanduser().resolve()

    if not parser_root.is_dir():
        print(f"Parser source directory does not exist: {parser_root}", file=sys.stderr)
        return 1
    if not mihomo.is_file():
        print(f"Mihomo executable does not exist: {mihomo}", file=sys.stderr)
        return 1

    sources = load_kotlin_sources(parser_root)
    domains = collect_domain_suffixes(sources)
    if not domains:
        print("No manga domain suffixes were extracted.", file=sys.stderr)
        return 1

    yaml_output = arguments.output.expanduser().resolve()
    mrs_output = arguments.mrs_output.expanduser().resolve()
    if yaml_output == mrs_output:
        print("YAML and MRS outputs must use distinct paths.", file=sys.stderr)
        return 1

    write_rule_provider(yaml_output, domains)
    try:
        write_mrs_rule_provider(mihomo, yaml_output, mrs_output)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Failed to generate MRS rule provider: {error}", file=sys.stderr)
        return 1

    for output in (yaml_output, mrs_output):
        try:
            display_path = output.relative_to(RULES_ROOT)
        except ValueError:
            display_path = output
        print(f"Wrote {len(domains)} domain suffixes to {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
