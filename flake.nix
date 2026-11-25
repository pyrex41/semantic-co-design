{
  description = "Specialize executable Shen and Maude behavior specifications to native code";

  inputs.capcov.url = "github:pyrex41/capcov/23f7841ca7369bf793c633e682fb00c77afc644c";
  inputs.nixpkgs.follows = "capcov/nixpkgs";

  outputs = { self, nixpkgs, capcov }:
    let
      eachSystem = nixpkgs.lib.genAttrs [ "aarch64-darwin" "aarch64-linux" "x86_64-linux" ];
      environment = system:
        let pkgs = nixpkgs.legacyPackages.${system};
            shenJit = pkgs.callPackage ./nix/shen-jit.nix { };
            hegel = pkgs.python3Packages.callPackage ./nix/hegel.nix { };
        in {
          nativeBuildInputs = [ pkgs.clang (pkgs.python3.withPackages (p: [ p.wasmtime hegel ])) pkgs.maude shenJit pkgs.lld ];
          buildInputs = [ pkgs.sqlite ];
          SHEN_JIT = "${shenJit}/bin/shen-jit";
          # The wrapped compiler targets the host; wasm32 needs the bare driver and wasm-ld.
          WASM_CLANG = "${pkgs.llvmPackages.clang-unwrapped}/bin/clang";
          PYTHONDONTWRITEBYTECODE = "1";
        };
      # experiments/unkernel_probe: Linux as a wasm module under Node, both pinned.
      unkernel = system: let pkgs = nixpkgs.legacyPackages.${system}; in {
        nativeBuildInputs = [ pkgs.nodejs_24 pkgs.python3 ];
        UNKERNEL = "${self.packages.${system}.unkernel}";
      };
      # experiments/unkernel_probe/guest_node_*: the tool's environment, Node with the pinned unkernel, and zig.
      ukNode = system: let env = environment system; pkgs = nixpkgs.legacyPackages.${system}; in env // {
        nativeBuildInputs = env.nativeBuildInputs ++ [ pkgs.nodejs_24 ];
        inherit (unkernel system) UNKERNEL;
        ZIG = "${pkgs.zig}/bin/zig";
      };
      # nix/node.nix: the node runtime, the conformance command and one package per example.
      packed = system: import ./nix/node.nix {
        pkgs = nixpkgs.legacyPackages.${system};
        src = self;
        environment = environment system;
      };
    in {
      packages = eachSystem (system: let p = packed system; in {
        node-runtime = p.runtime;
        node-conform = p.conform;
        accepted_job = p.node { example = "accepted_job"; };
        accepted_job-reply-before-ack = p.node { example = "accepted_job"; mutant = "reply-before-ack"; };
        heartbeat = p.node { example = "heartbeat"; nodes = 2; };
        unkernel = nixpkgs.legacyPackages.${system}.callPackage ./nix/unkernel.nix { };
      });
      nixosModules.default = ./nix/module.nix;
      devShells = eachSystem (system: {
        # Everything a check below is run with: the tool's environment, and Node with the pinned unkernel.
        default = let env = environment system; in nixpkgs.legacyPackages.${system}.mkShell (env // {
          nativeBuildInputs = env.nativeBuildInputs ++ [ nixpkgs.legacyPackages.${system}.nodejs_24 ];
          inherit (unkernel system) UNKERNEL;
        });
        research = let env = environment system; in nixpkgs.legacyPackages.${system}.mkShell (env // {
          nativeBuildInputs = env.nativeBuildInputs ++ [ capcov.packages.${system}.bifrost nixpkgs.legacyPackages.${system}.git ];
          PYTHONPATH = "${capcov}/src";
          BIFROST_SHEN_GO = "${capcov.packages.${system}.shen-go}/bin/shen";
        });
        # The probe's own inputs and nothing else; default has them too.
        unkernel = nixpkgs.legacyPackages.${system}.mkShell (unkernel system);
        # experiments/unkernel_probe/guest_node_*: default plus zig, the cross compiler that builds
        # the node as a static aarch64-linux program on any host (no Linux builder needed).
        uk-node = nixpkgs.legacyPackages.${system}.mkShell (ukNode system);
      });
      checks = eachSystem (system: {
        staged-examples = nixpkgs.legacyPackages.${system}.runCommand "staged-examples" (environment system) ''
          cd ${self}
          python3 -m unittest specializer.test_backend
          python3 -m specializer.check_backend_contract
          python3 -m examples.check_artifacts
          for example in vm decoder railway query; do
            python3 -m examples."$example".check
          done
          python3 -m experiments.branching_vm.check
          touch "$out"
        '';
        # One check per rung of WASM.md. Rung 1: the wasm step under the simulator.
        wasm-step = nixpkgs.legacyPackages.${system}.runCommand "wasm-step" (environment system) ''
          cd ${self}
          export HOME=$TMPDIR
          NODE_TARGET=wasm python3 -m tool check kernel accepted_job
          touch "$out"
        '';
        # Rung 2: schedules generated and shrunk by Hegel, on the host target (wasm-step runs them on the module).
        hegel-step = nixpkgs.legacyPackages.${system}.runCommand "hegel-step" (environment system) ''
          cd ${self}
          export HOME=$TMPDIR
          python3 -c 'import hegel; print("hegeltest", hegel.__version__, "libhegel", hegel.engine_version())'
          python3 -m tool check accepted_job
          touch "$out"
        '';
        # Rung 3: real processes on loopback, killed and restarted; needs no network beyond localhost.
        real-host = nixpkgs.legacyPackages.${system}.runCommand "real-host"
          (environment system // { __darwinAllowLocalNetworking = true; }) ''
          cd ${self}
          export HOME=$TMPDIR
          python3 -m tool real heartbeat accepted_job
          touch "$out"
        '';
        # Rung 3 again: the packages the machine tests deploy, driven by the same commands on loopback.
        packed-node = nixpkgs.legacyPackages.${system}.runCommand "packed-node"
          { nativeBuildInputs = [ nixpkgs.legacyPackages.${system}.python3 ]; __darwinAllowLocalNetworking = true; } ''
          export HOME=$TMPDIR
          python3 ${./nix/packed.py} ${(packed system).runtime}/bin/node-run \
            ${(packed system).conform}/bin/node-conform ${self.packages.${system}.accepted_job} \
            ${self.packages.${system}.accepted_job-reply-before-ack} ${self.packages.${system}.heartbeat}
          touch "$out"
        '';
        # Rung 5: two boots of Linux-as-wasm under a controlled host must reach the same memory, byte for byte.
        # About a minute of one core; the package is a fixed-output fetch, so nothing is fetched while it runs.
        unkernel-probe = nixpkgs.legacyPackages.${system}.runCommand "unkernel-probe" (unkernel system) ''
          export HOME=$TMPDIR
          python3 ${self}/experiments/unkernel_probe/check.py --no-stock
          touch "$out"
        '';
        # Rung 5 with the node in it: accepted_job as a static aarch64-linux program inside the guest equals
        # the native node line for line, and three boots are identical. About a minute; nothing is fetched.
        uk-node = nixpkgs.legacyPackages.${system}.runCommand "uk-node" (ukNode system) ''
          export HOME=$TMPDIR
          python3 ${self}/experiments/unkernel_probe/guest_node_check.py --quick
          touch "$out"
        '';
        native-replay = nixpkgs.legacyPackages.${system}.runCommand "native-replay" (environment system) ''
          python3 ${self}/logic/check.py
          python3 ${self}/native/check_accepted_job.py
          touch "$out"
        '';
      } // nixpkgs.lib.optionalAttrs (nixpkgs.lib.hasSuffix "-linux" system)
        # Rung 4: NixOS machine tests (real-vm, real-vm-mutant). They need KVM and a Linux builder.
        (import ./nix/vm.nix { pkgs = nixpkgs.legacyPackages.${system}; packages = packed system; }));
    };
}
