"""Translate only CEL label conjunctions that AM can express without widening."""

import ast
import json

import celpy
from lark import Tree


class UnsupportedSelector(ValueError):
    pass


def label_matcher(name, value):
    return {"name": name, "value": value, "isRegex": False, "isEqual": True}


def filter_matchers(expression):
    def unwrap(node):
        wrappers = {"expr", "conditionalor", "conditionaland", "relation", "addition",
                    "multiplication", "unary", "member", "primary", "paren_expr"}
        while isinstance(node, Tree) and str(node.data) in wrappers and len(node.children) == 1:
            node = node.children[0]
        return node

    def string(node):
        node = unwrap(node)
        if not isinstance(node, Tree) or node.data != "literal":
            raise UnsupportedSelector("unsupported_filter")
        value = ast.literal_eval(str(node.children[0]))
        if not isinstance(value, str) or not value:
            # AM treats absent labels as empty strings; CEL does not.
            raise UnsupportedSelector("unsupported_filter")
        return value

    def label(node):
        node = unwrap(node)
        if not isinstance(node, Tree) or node.data not in {"member_index", "member_dot"}:
            raise UnsupportedSelector("unsupported_filter")
        base = unwrap(node.children[0])
        if not isinstance(base, Tree) or base.data != "ident" or str(base.children[0]) != "labels":
            raise UnsupportedSelector("unsupported_filter")
        return string(node.children[1]) if node.data == "member_index" else str(node.children[1])

    def parse(node):
        node = unwrap(node)
        if isinstance(node, Tree) and node.data == "conditionaland" and len(node.children) == 2:
            return parse(node.children[0]) + parse(node.children[1])
        if (isinstance(node, Tree) and node.data == "relation" and len(node.children) == 2
                and isinstance(node.children[0], Tree) and node.children[0].data == "relation_eq"):
            return [label_matcher(label(node.children[0].children[0]), string(node.children[1]))]
        raise UnsupportedSelector("unsupported_filter")

    try:
        matchers = parse(celpy.Environment().compile(expression))
    except (ValueError, TypeError, SyntaxError, celpy.CELParseError, IndexError, RecursionError):
        raise UnsupportedSelector("unsupported_filter") from None
    return sorted(matchers, key=lambda item: (item["name"], item["value"]))


def matchers_to_cel(matchers):
    if not isinstance(matchers, list) or not matchers:
        raise ValueError("Empty Alertmanager matcher set")
    clauses = []
    for matcher in matchers:
        if not isinstance(matcher, dict) or type(matcher.get("isRegex", False)) is not bool or type(matcher.get("isEqual", True)) is not bool:
            raise ValueError("Invalid Alertmanager matcher")
        name, value = matcher.get("name"), matcher.get("value")
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise ValueError("Invalid Alertmanager matcher")
        key = f'labels[{json.dumps(name)}]'
        operand = f'(has({key}) ? {key} : "")'
        if matcher.get("isRegex", False):
            import re2
            # Absolute RE2 anchors preserve AM's full-string matcher semantics,
            # including Unicode classes, inline flags and trailing newlines.
            pattern = r"\A(?:" + value + r")\z"
            try:
                options = re2.Options()
                options.log_errors = False
                re2.compile(pattern, options=options)
            except re2.error:
                raise ValueError("Unsupported Alertmanager regex") from None
            predicate = f'{operand}.matches({json.dumps(pattern)})'
        else:
            predicate = f'{operand} == {json.dumps(value)}'
        clauses.append(f'({predicate})' if matcher.get("isEqual", True) else f'!({predicate})')
    return " && ".join(clauses)
