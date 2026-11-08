#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JS 混淆器 —— 按 obfuscation_rules.json 规则执行，纯标准库、确定性（seed）。

核心变换（均为"值保持"变换，不改变运行逻辑）：
  1. 字符串切割   ：长度达标的字符串字面量被随机切成多段并用 + 连接。
  2. 字典替换     ：命中字典（整值/子串）的字符串字面量按转义模式重编码；
                    可选（默认关）标识符重命名。
  3. 属性访问改写 ：a.b -> a['b']（字典键或全部属性）。

输出：Genesis.txt（如提供，自动识别 /* */ 注释块）在前 + 混淆后代码在后。

用法：
  python3 obfuscate.py --config obfuscation_rules.json \
                       --input _worker.js --genesis Genesis.txt \
                       --output _worker_obfus.js
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

# ---------------- 词法分析 ----------------

KEYWORDS = {
    'break', 'case', 'catch', 'class', 'const', 'continue', 'debugger', 'default',
    'delete', 'do', 'else', 'export', 'extends', 'finally', 'for', 'function',
    'if', 'import', 'in', 'instanceof', 'let', 'new', 'of', 'return', 'static',
    'super', 'switch', 'this', 'throw', 'try', 'typeof', 'var', 'void', 'while',
    'with', 'yield', 'await', 'enum', 'async', 'get', 'set', 'null', 'true',
    'false', 'as', 'from', 'target',
}

_IDENT_RE = re.compile(r'[A-Za-z_$\w]+', re.UNICODE)

# 这些 token 之后出现的 / 视为正则字面量开始
REGEX_PRECEDING = {
    None, '(', '[', '{', ',', ';', ':', '=', '!', '&', '|', '?', '+', '-', '*',
    '/', '%', '<', '>', '^', '~', '=>', '&&', '||', '??', '==', '!=', '===',
    '!==', '<=', '>=', '<<', '>>', '>>>', '+=', '-=', '*=', '/=', '%=', '&=',
    '|=', '^=', '**', '...', '?.', 'return', 'typeof', 'instanceof', 'in', 'of',
    'new', 'delete', 'void', 'throw', 'case', 'do', 'else', 'yield', 'await',
    'extends',
}

# 这些 token 之后的字符串字面量不参与切割（指令序言/类字段/对象键/module 说明符等结构位置）
SPLIT_SKIP_PRECEDING = {';', '{', '(', 'get', 'set', 'import', 'from'}

_NUMBER_RE = re.compile(
    r'0[xX][0-9a-fA-F]+n?|0[bB][01]+n?|0[oO][0-7]+n?|'
    r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?n?'
)
_MULTI_OPS = (
    '===', '!==', '**=', '<<=', '>>=', '>>>=', '...', '=>', '==', '!=', '<=',
    '>=', '&&', '||', '??', '?.', '+=', '-=', '*=', '/=', '%=', '&=', '|=',
    '^=', '<<', '>>', '>>>', '**', '++', '--',
)

_SKIP_KINDS = {'ws', 'line_comment', 'block_comment'}


class Token:
    __slots__ = ('kind', 'text', 'value')

    def __init__(self, kind, text, value=None):
        self.kind = kind      # ws|line_comment|block_comment|string|template|regex|number|ident|punct|eof
        self.text = text
        self.value = value    # string: 解码后的值；ident: 名称


def tokenize(src):
    """把 JS 源码切分为 token。除字符串外一切原样保留，保证输出与输入逐字节对应。"""
    tokens = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            j = i + 1
            while j < n and src[j].isspace():
                j += 1
            tokens.append(Token('ws', src[i:j]))
            i = j
        elif src.startswith('//', i):
            j = src.find('\n', i)
            j = n if j == -1 else j
            tokens.append(Token('line_comment', src[i:j]))
            i = j
        elif src.startswith('/*', i):
            j = src.find('*/', i + 2)
            j = (j + 2) if j != -1 else n
            tokens.append(Token('block_comment', src[i:j]))
            i = j
        elif c in '\'"':
            j, value = _scan_string(src, i)
            tokens.append(Token('string', src[i:j], value))
            i = j
        elif c == '`':
            # 模板字符串不做变换，原样保留（含嵌套模板的场景保守处理）
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                elif src[j] == '`':
                    j += 1
                    break
                else:
                    j += 1
            tokens.append(Token('template', src[i:j]))
            i = j
        else:
            m = _IDENT_RE.match(src, i)
            if m:
                text = m.group(0)
                tokens.append(Token('ident', text, text))
                i = m.end()
                continue
            m = _NUMBER_RE.match(src, i)
            if m:
                tokens.append(Token('number', m.group(0)))
                i = m.end()
                continue
            if c == '/' and _regex_allowed(tokens):
                j = _scan_regex(src, i)
                if j > i + 1:
                    tokens.append(Token('regex', src[i:j]))
                    i = j
                    continue
            for op in _MULTI_OPS:
                if src.startswith(op, i):
                    tokens.append(Token('punct', op))
                    i += len(op)
                    break
            else:
                tokens.append(Token('punct', c))
                i += 1
    tokens.append(Token('eof', ''))
    return tokens


def _regex_allowed(tokens):
    prev = None
    for t in reversed(tokens):
        if t.kind in _SKIP_KINDS:
            continue
        prev = t
        break
    if prev is None:
        return True
    if prev.kind in ('ident', 'number', 'string', 'template', 'regex'):
        return prev.text in REGEX_PRECEDING  # 仅关键字可后接正则
    if prev.kind == 'punct':
        return prev.text in REGEX_PRECEDING
    return False


def _scan_regex(src, i):
    """扫描正则字面量，返回结束下标；无法构成正则时返回 i+1（作为除法处理）。"""
    j, n = i + 1, len(src)
    in_class = False
    while j < n:
        c = src[j]
        if c == '\\':
            j += 2
        elif c == '\n':
            return i + 1
        elif c == '[':
            in_class = True
            j += 1
        elif c == ']':
            in_class = False
            j += 1
        elif c == '/' and not in_class:
            j += 1
            while j < n and ('a' <= src[j] <= 'z' or 'A' <= src[j] <= 'Z'):
                j += 1
            return j
        else:
            j += 1
    return i + 1


def _scan_string(src, i):
    """扫描字符串字面量，返回 (结束下标, 解码后的值)。"""
    quote, n = src[i], len(src)
    j = i + 1
    chars = []
    while j < n:
        c = src[j]
        if c == '\\':
            if j + 1 >= n:
                j += 1
                continue
            e = src[j + 1]
            if e in '\'"\\bfnrtv':
                chars.append({'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r',
                              't': '\t', 'v': '\v'}.get(e, e))
                j += 2
            elif e == 'x':
                if j + 4 <= n:
                    try:
                        chars.append(chr(int(src[j + 2:j + 4], 16)))
                    except ValueError:
                        pass
                    j += 4
                else:
                    j += 2
            elif e == 'u':
                if j + 2 < n and src[j + 2] == '{':
                    k = src.find('}', j + 3)
                    if k != -1:
                        try:
                            chars.append(chr(int(src[j + 3:k], 16)))
                        except ValueError:
                            pass
                        j = k + 1
                    else:
                        j += 2
                elif j + 6 <= n:
                    try:
                        chars.append(chr(int(src[j + 2:j + 6], 16)))
                    except ValueError:
                        pass
                    j += 6
                else:
                    j += 2
            elif e == '\n':
                j += 2          # 行连接符，无字符
            elif e == '\r':
                j += 3 if (j + 2 < n and src[j + 2] == '\n') else 2
            elif e == '0':
                chars.append('\0')
                j += 2
            else:
                chars.append(e)  # 非法转义：JS 宽松模式下即为该字符
                j += 2
        elif c == quote:
            j += 1
            break
        elif c in '\n\r':
            chars.append(c)      # 未转义换行（非法 JS，原样容忍）
            j += 1
        else:
            chars.append(c)
            j += 1
    return j, ''.join(chars)


def _build_sig_links(tokens):
    """为每个 token 记录前后最近的有效（非空白/注释）token 下标。"""
    n = len(tokens)
    prev_sig, next_sig = [-1] * n, [-1] * n
    last = -1
    for i, t in enumerate(tokens):
        prev_sig[i] = last
        if t.kind not in _SKIP_KINDS:
            last = i
    nxt = -1
    for i in range(n - 1, -1, -1):
        next_sig[i] = nxt
        if tokens[i].kind not in _SKIP_KINDS:
            nxt = i
    return prev_sig, next_sig


# ---------------- 编码工具 ----------------

def _escape_char(ch):
    """把单个字符转义为十六进制转义序列（代理对拆成两个转义）。"""
    o = ord(ch)
    if 0x20 <= o <= 0x7E:
        return '\\x%02x' % o
    if o <= 0xFFFF:
        return '\\u%04x' % o
    b = ch.encode('utf-16-be')
    return '\\u%04x\\u%04x' % (int.from_bytes(b[0:2], 'big'), int.from_bytes(b[2:4], 'big'))


def _reencode(value, spans, quote):
    """按 spans（None 表示全部）转义字符串值，其余字符按普通转义规则输出。"""
    out = []
    all_ = spans is None
    idx = 0
    for ch in value:
        hit = all_ or any(s <= idx < e for s, e in spans)
        if hit:
            out.append(_escape_char(ch))
        else:
            o = ord(ch)
            if ch == '\\':
                out.append('\\\\')
            elif ch == quote:
                out.append('\\' + quote)
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            elif 0x20 <= o < 0x7F:
                out.append(ch)
            else:
                out.append(_escape_char(ch))
        idx += 1
    return quote + ''.join(out) + quote


def _encode_chunk(piece, quote):
    """按标准转义规则编码一个切割片段。"""
    out = []
    for ch in piece:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == quote:
            out.append('\\' + quote)
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif 0x20 <= o < 0x7F:
            out.append(ch)
        else:
            out.append(_escape_char(ch))
    return quote + ''.join(out) + quote


# ---------------- 混淆主流程 ----------------

def _dict_hit(value, dictionary, match_substrings, keys_by_len):
    """字典命中检测：整值优先，其次最长子串。返回命中词或 None。"""
    low = value.lower()
    if low in dictionary:
        return low
    if not match_substrings:
        return None
    for key in keys_by_len:
        if key in low:
            return key
    return None


def _split_value(value, ss, rng):
    min_chunks = max(2, ss.get('min_chunks', 2))
    max_chunks = ss.get('max_chunks', 5)
    min_chunk = max(1, ss.get('min_chunk_length', 4))
    length = len(value)
    if length < min_chunks * min_chunk:
        return [value]
    hi = min(max_chunks, length // min_chunk)
    lo = min(min_chunks, hi)
    if hi < lo or lo < 2:
        return [value]
    k = rng.randint(lo, hi)
    candidates = list(range(min_chunk, length - min_chunk + 1))
    rng.shuffle(candidates)
    cuts = sorted(candidates[:k - 1])
    pieces, start = [], 0
    for c in cuts + [length]:
        pieces.append(value[start:c])
        start = c
    return pieces


def obfuscate(src, rules):
    rng = random.Random(rules.get('seed', 0))
    ss = rules.get('string_splitting', {})
    dr = rules.get('dictionary_replacement', {})
    pa = rules.get('property_access', {})
    dictionary = {k.lower(): v for k, v in dr.get('dictionary', {}).items()}
    match_substrings = dr.get('match_substrings', True)
    escape_mode = dr.get('escape_mode', 'keyword')
    exclude_literals = set(ss.get('exclude_literals', []))
    protected = set(dr.get('protected', [])) | set(dictionary.keys())
    keys_by_len = sorted(dictionary.keys(), key=len, reverse=True)

    tokens = tokenize(src)
    prev_sig, next_sig = _build_sig_links(tokens)
    out = []
    stats = {'split': 0, 'escaped': 0, 'renamed': 0, 'bracketed': 0}

    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        kind = t.kind

        if kind == 'eof':
            break

        if kind == 'string':
            value = t.value
            ps = tokens[prev_sig[i]] if prev_sig[i] >= 0 else None
            ns = tokens[next_sig[i]] if next_sig[i] >= 0 else None
            prev_text = ps.text if ps is not None else None
            excluded = value in exclude_literals
            is_key = prev_text in ('{', ',') and ns is not None and ns.text == ':'

            # 1) 字典替换（字符串字面量重编码，值保持）
            hit = None
            if (dr.get('enabled', True) and dr.get('apply_to_string_literals', True)
                    and escape_mode != 'none' and not excluded):
                hit = _dict_hit(value, dictionary, match_substrings, keys_by_len)
            if hit is not None:
                spans = None
                if escape_mode == 'keyword':
                    low, spans = value.lower(), []
                    start = 0
                    while True:
                        p = low.find(hit, start)
                        if p == -1:
                            break
                        spans.append((p, p + len(hit)))
                        start = p + 1
                out.append(_reencode(value, spans, t.text[0]))
                stats['escaped'] += 1
                i += 1
                continue

            # 2) 字符串切割（值保持；结构位置自动跳过）
            skip_split = (excluded or ps is None or is_key
                          or prev_text in SPLIT_SKIP_PRECEDING)
            if (ss.get('enabled', True) and not skip_split
                    and len(value) >= ss.get('min_length', 12)):
                pieces = _split_value(value, ss, rng)
                if len(pieces) > 1:
                    quote = t.text[0]
                    out.append(' + '.join(_encode_chunk(p, quote) for p in pieces))
                    stats['split'] += 1
                    i += 1
                    continue
            out.append(t.text)
            i += 1
            continue

        if kind == 'ident':
            renamed = False
            if dr.get('enabled', True) and dr.get('apply_to_identifiers', False):
                key = t.value.lower()
                ps = tokens[prev_sig[i]] if prev_sig[i] >= 0 else None
                is_property = ps is not None and ps.text in ('.', '?.')
                if (key in dictionary and key not in protected
                        and (not is_property or dr.get('rename_properties', False))):
                    out.append(dictionary[key])
                    stats['renamed'] += 1
                    renamed = True
            if not renamed:
                out.append(t.text)
            i += 1
            continue

        if kind == 'punct' and t.text in ('.', '?.') and pa.get('enabled', True):
            ps = tokens[prev_sig[i]] if prev_sig[i] >= 0 else None
            ns = tokens[next_sig[i]] if next_sig[i] >= 0 else None
            ok_prev = ps is not None and (
                (ps.kind == 'ident' and ps.text not in ('new', 'import'))
                or ps.kind in ('string', 'template')
                or (ps.kind == 'punct' and ps.text in (')', ']', '?.'))
            )
            if ok_prev and ns is not None and ns.kind == 'ident':
                key = ns.value
                if pa.get('bracket_all', False) or (pa.get('bracket_dictionary_keys', True)
                                                    and key.lower() in dictionary):
                    quoted = "'" + key.replace('\\', '\\\\').replace("'", "\\'") + "'"
                    out.append('?.[' + quoted + ']' if t.text == '?.' else '[' + quoted + ']')
                    stats['bracketed'] += 1
                    i += 2
                    continue
            out.append(t.text)
            i += 1
            continue

        out.append(t.text)
        i += 1

    return ''.join(out), stats


# ---------------- Genesis 合并 ----------------

def wrap_genesis(raw):
    """Genesis 文本：若已是完整 /* */ 注释块则原样使用，否则包装为注释块。"""
    stripped = raw.strip()
    if stripped.startswith('/*') and stripped.endswith('*/'):
        return raw
    return '/*\n' + raw.replace('*/', '* /') + '\n*/\n'


def main():
    ap = argparse.ArgumentParser(description='按规则混淆 JS 并合并 Genesis')
    ap.add_argument('--config', required=True, help='混淆规则 JSON 配置文件')
    ap.add_argument('--input', required=True, help='待混淆的 JS 文件（如 _worker.js）')
    ap.add_argument('--genesis', help='Genesis 文本文件（可选，置于输出文件开头）')
    ap.add_argument('--output', required=True, help='输出文件（如 _worker_obfus.js）')
    args = ap.parse_args()

    rules = json.loads(Path(args.config).read_text(encoding='utf-8'))
    src = Path(args.input).read_text(encoding='utf-8')
    obfuscated, stats = obfuscate(src, rules)

    parts = []
    if args.genesis:
        parts.append(wrap_genesis(Path(args.genesis).read_text(encoding='utf-8')))
    parts.append(obfuscated)

    with open(args.output, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(parts))

    print('obfuscate stats: ' + json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
