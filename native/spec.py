"""Run or specialize a deterministic finite behavioral specification."""
import argparse
import json
import re
import subprocess
import sqlite3
from contextlib import closing
from pathlib import Path


def load(path, bindings):
    spec = json.loads(path.read_text())
    if "runtime_digest" in spec:
        raise ValueError("runtime_digest is reserved for compiler use")
    parameters = spec.get("parameters", {})
    if bindings.keys() - parameters.keys():
        raise ValueError("unknown parameter")
    values = {key: bindings.get(key, options[0]) for key, options in parameters.items()}
    if any(values[k] not in options for k, options in parameters.items()):
        raise ValueError("invalid parameter value")
    if "types" in spec:
        for rule in spec["rules"]:
            target = rule[3]
            constructor = target[0] if isinstance(target, list) else target
            if constructor.startswith("$"):
                selected = values[constructor[1:]]
                rule[3] = [selected, *target[1:]] if isinstance(target, list) else selected
        from finite import expand
        if "sha256" in spec["types"].values():
            from digest import prepare
            spec = prepare(spec)
        else:
            spec = expand(spec)
    if "storage" in spec and spec["storage"] != {"effect": "keyed-state", "key": "uint32"}:
        raise ValueError("unsupported storage declaration")
    states, events = spec["states"], spec["events"]
    for names in (states, events):
        if not names or len(names) != len(set(names)):
            raise ValueError("expected distinct names")
    if spec["initial"] not in states:
        raise ValueError("invalid initial state")
    rules = {}
    for state, event, target, output in spec["rules"]:
        if target.startswith("$"):
            target = values[target[1:]]
        if state not in states or target not in states or event not in events or (state,event) in rules:
            raise ValueError("invalid or ambiguous transition")
        rules[state,event] = target, output
    for name in [*states, *events, *(out for _, out in rules.values())]:
        if not re.fullmatch("[a-z][a-z0-9_-]*", name):
            raise ValueError("invalid symbol")
    return spec, rules


def apply(spec, rules, state, event):
    if spec.get("runtime_digest"):
        from digest import step
        return step(spec, rules, state, event)
    if event not in spec["events"] or state not in spec["states"]:
        raise ValueError("unknown event or stored state")
    return rules.get((state, event), (state, "ignored"))


def run(spec, rules, events):
    state = spec["initial"]
    for event in events:
        state, output = apply(spec, rules, state, event)
        print(state, output)


def run_durable(spec, rules, events, database, key):
    if not key or not re.fullmatch("[0-9]+", key) or int(key) > 2**32 - 1:
        raise ValueError("expected uint32 key")
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY,state TEXT NOT NULL)")
        for event in events:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT state FROM instances WHERE id=?", (int(key),)).fetchone()
                state = row[0] if row else spec["initial"]
                target, output = apply(spec, rules, state, event)
                db.execute("INSERT INTO instances VALUES(?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                           (int(key), target))
            print(target, output)


def transition_source(spec, rules):
    lines = ['#include <stdio.h>', '#include <string.h>',
             f'static const char *initial = "{spec["initial"]}";',
             'static int step(const char *state,const char *event,const char **next,const char **out){']
    for state in spec["states"]:
        lines.append(f'if (!strcmp(state,"{state}")) {{')
        for event in spec["events"]:
            target, observation = rules.get((state,event), (state,"ignored"))
            lines.append(f'if (!strcmp(event,"{event}")) {{ *next="{target}"; *out="{observation}"; return 1; }}')
        lines.append('}')
    lines.append('return 0;}')
    return lines


def compile_native(spec, rules, output):
    source = transition_source(spec, rules)
    if spec.get("runtime_digest"):
        from digest import wrapper
        source = [line.replace("int step(", "int symbol_step(") for line in source]
        source.extend(wrapper(spec))
    libraries = []
    if "storage" in spec:
        source.append(Path(__file__).with_name("store.c").read_text())
        libraries.append("-lsqlite3")
    else:
        source.extend(['int main(int argc,char **argv){const char *state=initial,*out;',
                       'for(int i=1;i<argc;i++){if(!step(state,argv[i],&state,&out)) return 2;',
                       'printf("%s %s\\n",state,out);} return 0;}'])
    subprocess.run(['clang','-std=c11','-O2','-x','c','-',*libraries,'-o',str(output)],
                   input='\n'.join(source), text=True, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec', type=Path)
    parser.add_argument('--bind', action='append', default=[], metavar='KEY=VALUE')
    parser.add_argument('--database', type=Path)
    parser.add_argument('--key')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run', nargs='*', metavar='EVENT')
    mode.add_argument('--compile', type=Path, metavar='EXECUTABLE')
    args = parser.parse_args()
    spec, rules = load(args.spec, dict(item.split('=',1) for item in args.bind))
    if args.compile:
        compile_native(spec, rules, args.compile)
    elif "storage" in spec:
        if args.database is None or args.key is None or not args.run:
            parser.error("durable execution requires --database, --key and at least one event")
        run_durable(spec, rules, args.run, args.database, args.key)
    else:
        run(spec, rules, args.run)
