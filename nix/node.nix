# The compiled node as packages: what a machine needs to run it and what a judge needs to check it.
{ pkgs, src, environment }:
rec {
  python = pkgs.python3.withPackages (p: [ p.wasmtime ]);

  # tool/ and the backend it imports, with two launchers. Nothing in it knows an example.
  #   node-host <packed directory> <node> <data> <client host:port> <peer host:port> ...
  #   node-run <module under tool> <arguments>      e.g. node-run host.ask node0:7001 5 '["status", 1]'
  runtime = pkgs.runCommand "node-runtime" { } ''
    mkdir -p $out/lib $out/bin
    cp -R ${src}/tool ${src}/specializer $out/lib/
    cat > $out/bin/node-run <<EOF
    #!${pkgs.runtimeShell}
    module=\$1; shift
    PYTHONPATH=$out/lib PYTHONDONTWRITEBYTECODE=1 exec ${python}/bin/python3 -m "tool.\$module" "\$@"
    EOF
    cat > $out/bin/node-host <<EOF
    #!${pkgs.runtimeShell}
    exec $out/bin/node-run host "\$@"
    EOF
    chmod +x $out/bin/node-run $out/bin/node-host
  '';

  # tool/host/conform.py with the shen-jit it replays through.
  conform = pkgs.writeShellScriptBin "node-conform" ''
    SHEN_JIT=${environment.SHEN_JIT} exec ${runtime}/bin/node-run host.conform "$@"
  '';

  # share/node is what tool/host/pack.py writes; bin/node-host runs one node of it:
  #   node-host <node> <data> <client host:port> <peer host:port> ...
  node = { example, nodes ? 1, mutant ? null }:
    pkgs.runCommand "${example}${pkgs.lib.optionalString (mutant != null) "-${mutant}"}-node" environment ''
      export HOME=$TMPDIR
      cd ${src}
      python3 -m tool.host.pack ${example} $out/share/node ${toString nodes} ${toString mutant}
      mkdir -p $out/bin
      cat > $out/bin/node-host <<EOF
      #!${pkgs.runtimeShell}
      exec ${runtime}/bin/node-host $out/share/node "\$@"
      EOF
      chmod +x $out/bin/node-host
    '';
}
