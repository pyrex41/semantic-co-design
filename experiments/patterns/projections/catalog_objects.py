"""Check bounded object projections against the original linked GoF C APIs.

Only the original operation is claimed as parity. The projection's edited
programs often model changes which the original API does not expose. The
consumer adapts representations (packing, decimal versus byte pairs, and
flyweight lookup); it never fabricates a target call or a returned value.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ..gof.build import CFLAGS, compile_library
from .check import require, run
from .check_core import oracle
from .cases.gof_objects import CASES

PROFILE = {
    'factory': {'pattern': 'abstract_factory', 'family': 'offset', 'key': 3},
    'sequence': {'pattern': 'builder', 'representation': 'sequence', 'capacity': 5},
    'summary': {'pattern': 'builder', 'representation': 'summary', 'capacity': 5},
    'method': {'pattern': 'factory_method', 'product': 'offset', 'prepare': 2, 'parameter': 3, 'finish': 1},
    'prototype': {'pattern': 'prototype', 'kind': 1, 'values': [12, 20]},
    'singleton': {'pattern': 'singleton', 'revision': 4, 'limit': 1},
    'adapter': {'pattern': 'adapter', 'format': 'pair8', 'target': 'target_pair'},
    'bridge': {'pattern': 'bridge', 'backend': {'read': 'read_channel', 'channel': 2}, 'adjustment': 3, 'threshold': 11},
    'composite': {'pattern': 'composite', 'read': 'read_leaf', 'tree': {'op': 'sum', 'children': [1, {'op': 'max', 'children': [2, 1]}]}},
    'decorator': {'pattern': 'decorator', 'target': 'component', 'layers': [
        {'before': {'op': 'add', 'value': 2}, 'after': {'op': 'add', 'value': 2}},
        {'before': {'op': 'multiply', 'value': 3}, 'after': {'op': 'multiply', 'value': 3}},
    ]},
    'facade': {'pattern': 'facade', 'validate': 'validate', 'encode': 'encode', 'send': 'send_value'},
    'flyweight': {'pattern': 'flyweight', 'descriptors': [
        {'scale': n, 'offset': 0, 'aliases': [f'd{n}']} for n in range(1, 5)
    ]},
    'proxy': {'pattern': 'proxy', 'roles': [2], 'target': 'target_request'},
}

PREAMBLE = r'''
#include "gof.h"
#include <stdio.h>
struct ctx { unsigned count, encoded; uint32_t leaves[3]; };
uint32_t target_pair(void *c, uint32_t a, uint32_t b) { (void)c; return 4*a+b; }
uint32_t read_channel(void *c, uint32_t ch, uint32_t request) { (void)c; return ch+request; }
uint32_t read_leaf(void *c, uint32_t id) { return ((struct ctx *)c)->leaves[id]; }
uint32_t component(void *c, uint32_t x) { (void)c; return x+4; }
int validate(void *c, uint32_t x) { (void)c; return x<10; }
uint32_t encode(void *c, uint32_t x) { struct ctx *s=c; ++s->count; return x+5; }
int send_value(void *c, uint32_t x) { struct ctx *s=c; ++s->count; s->encoded=x; return 1; }
uint32_t target_request(void *c, uint32_t x) { ++((struct ctx *)c)->count; return x+4; }
static const gof_flyweight_intrinsic *lookup(unsigned n) {
    switch(n) { case 1: return gof_flyweight_get_d1(); case 2: return gof_flyweight_get_d2();
    case 3: return gof_flyweight_get_d3(); case 4: return gof_flyweight_get_d4(); default: return 0; }
}
int main(void) {
'''


def body(slug, args):
    if slug == 'gof-abstract-factory':
        family, value = args
        if family:  # The original alternative is XOR, while this projection uses scale.
            return None
        return f'printf("%u\\n", gof_factory_read(gof_factory_make_reader(), gof_factory_write(gof_factory_make_writer(), {value})));'
    if slug == 'gof-builder':
        kind, *values = args
        vals = ','.join(map(str, values))
        if kind == 0:
            return f'{{ uint32_t a[] = {{{vals}}}; gof_sequence_product p=gof_sequence_build(a,{len(values)}); unsigned sum=0; for(unsigned i=0;i<p.count;i++) sum+=p.values[i]; printf("%u\\n",100*p.count+sum); }}'
        return f'{{ uint32_t a[] = {{{vals}}}; gof_summary_product p=gof_summary_build(a,{len(values)}); printf("%u\\n",100*p.count+p.max); }}'
    if slug == 'gof-factory-method':
        return f'printf("%u\\n", gof_method_create({args[0]}).value);'
    if slug == 'gof-prototype':
        w, r, delta = args
        return f'{{ gof_prototype_product a=gof_prototype_clone(), b=gof_prototype_clone(); b.values[{w}]+={delta}; printf("%u\\n",100*a.values[{r}]+b.values[{r}]); }}'
    if slug == 'gof-singleton':
        if len(args) < 2:  # A single retrieval cannot establish shared identity.
            return None
        values = ','.join(map(str, args))
        return f'{{ unsigned m[]={{{values}}}, mismatch=0, sum=0; const gof_singleton_configuration *first=gof_singleton_get(); for(unsigned i=0;i<{len(args)};i++) {{ const gof_singleton_configuration *p=gof_singleton_get(); mismatch += p != first; sum += m[i]*gof_singleton_revision(p); }} printf("%u\\n",10000+100*mismatch+sum); }}'
    if slug == 'gof-adapter':
        n = args[0]
        return f'printf("%u\\n", gof_adapter(0, {n//10}*256+{n%10}));'
    if slug == 'gof-bridge':
        x,y=args
        return f'printf("%u\\n", 100*gof_bridge_value(0,{x})+gof_bridge_ready(0,{y}));'
    if slug == 'gof-composite':
        x,y=args
        return f'{{ struct ctx c={{0}}; c.leaves[1]={x}; c.leaves[2]={y}; printf("%u\\n", gof_composite(&c)+1); }}'
    if slug == 'gof-decorator':
        return f'printf("%u\\n", gof_decorator(0,{args[0]}));'
    if slug == 'gof-facade':
        return f'{{ struct ctx c={{0}}; (void)gof_facade(&c,{args[0]}); printf("%u\\n", 100*c.count+c.encoded); }}'
    if slug == 'gof-flyweight':
        d1,p1,d2,p2,d3,p3=args
        return f'''{{ const gof_flyweight_intrinsic *a=lookup({d1}), *b=lookup({d2}), *c=lookup({d3});
            unsigned ib=(b==a)?1:2, ic=(c==a)?1:(c==b)?ib:(ib==1?2:3);
            unsigned value=gof_flyweight_apply(a,{p1})+gof_flyweight_apply(b,{p2})+gof_flyweight_apply(c,{p3})+2;
            printf("%u\\n", 100*(100+10*ib+ic)+value); }}'''
    if slug == 'gof-proxy':
        role,value=args
        return f'{{ struct ctx c={{0}}; uint32_t out=0; (void)gof_proxy(&c,{role},{value},&out); printf("%u\\n",100*c.count+out); }}'
    raise ValueError(slug)


def check_objects():
    expected = []
    lines = [PREAMBLE]
    covered = set()
    for case in CASES:
        for args, wanted, _ in case['samples']:
            observation = body(case['id'], args)
            if observation is None:
                continue
            require(oracle(case['program'], case['config'], args) == wanted,
                    f"projection sample mismatch: {case['id']} {args}")
            lines.append(observation)
            expected.append((case['id'], args, wanted))
            covered.add(case['id'])
    require(covered == {case['id'] for case in CASES}, 'missing object-pattern parity')
    lines.append('return 0; }')
    with TemporaryDirectory(prefix='projection-objects-') as directory:
        work = Path(directory)
        archive = work / 'archive'
        compile_library(PROFILE, archive)
        source = work / 'consumer.c'
        source.write_text('\n'.join(lines))
        executable = work / 'consumer'
        run([os.environ.get('CLANG', 'clang'), *CFLAGS, '-I', archive, source,
             archive / 'libgof.a', '-o', executable])
        observed = list(map(int, run([executable]).splitlines()))
    require(len(observed) == len(expected), 'object catalog output count mismatch')
    for (slug, args, wanted), actual in zip(expected, observed):
        require(actual == wanted, f'{slug} {args}: C={actual}, projection={wanted}')
    return len(expected)


if __name__ == '__main__':
    print(f'GoF object parity: {check_objects()} linked C observations')
