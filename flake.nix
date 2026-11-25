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
        in {
          nativeBuildInputs = [ pkgs.clang (pkgs.python3.withPackages (p: [ p.wasmtime ])) pkgs.maude shenJit pkgs.lld ];
          buildInputs = [ pkgs.sqlite ];
          SHEN_JIT = "${shenJit}/bin/shen-jit";
          # The wrapped compiler targets the host; wasm32 needs the bare driver and wasm-ld.
          WASM_CLANG = "${pkgs.llvmPackages.clang-unwrapped}/bin/clang";
          PYTHONDONTWRITEBYTECODE = "1";
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
      });
      nixosModules.default = ./nix/module.nix;
      devShells = eachSystem (system: {
        default = nixpkgs.legacyPackages.${system}.mkShell (environment system);
        research = let env = environment system; in nixpkgs.legacyPackages.${system}.mkShell (env // {
          nativeBuildInputs = env.nativeBuildInputs ++ [ capcov.packages.${system}.bifrost nixpkgs.legacyPackages.${system}.git ];
          PYTHONPATH = "${capcov}/src";
          BIFROST_SHEN_GO = "${capcov.packages.${system}.shen-go}/bin/shen";
        });
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
        wasm-step = nixpkgs.legacyPackages.${system}.runCommand "wasm-step" (environment system) ''
          cd ${self}
          export HOME=$TMPDIR
          NODE_TARGET=wasm python3 -m tool check kernel accepted_job
          touch "$out"
        '';
        # Real processes on loopback, killed and restarted; needs no network beyond localhost.
        real-host = nixpkgs.legacyPackages.${system}.runCommand "real-host"
          (environment system // { __darwinAllowLocalNetworking = true; }) ''
          cd ${self}
          export HOME=$TMPDIR
          python3 -m tool real heartbeat accepted_job
          touch "$out"
        '';
        # The packages the machine tests deploy, driven by the same commands on loopback.
        packed-node = nixpkgs.legacyPackages.${system}.runCommand "packed-node"
          { nativeBuildInputs = [ nixpkgs.legacyPackages.${system}.python3 ]; __darwinAllowLocalNetworking = true; } ''
          export HOME=$TMPDIR
          python3 ${./nix/packed.py} ${(packed system).runtime}/bin/node-run \
            ${(packed system).conform}/bin/node-conform ${self.packages.${system}.accepted_job} \
            ${self.packages.${system}.accepted_job-reply-before-ack} ${self.packages.${system}.heartbeat}
          touch "$out"
        '';
        native-replay = nixpkgs.legacyPackages.${system}.runCommand "native-replay" (environment system) ''
          python3 ${self}/logic/check.py
          python3 ${self}/native/check_accepted_job.py
          touch "$out"
        '';
      } // nixpkgs.lib.optionalAttrs (nixpkgs.lib.hasSuffix "-linux" system)
        # NixOS machine tests (real-vm, real-vm-mutant): they need KVM and a Linux builder.
        (import ./nix/vm.nix { pkgs = nixpkgs.legacyPackages.${system}; packages = packed system; }));
    };
}
