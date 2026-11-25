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
          nativeBuildInputs = [ pkgs.clang pkgs.python3 pkgs.maude shenJit ];
          buildInputs = [ pkgs.sqlite ];
          SHEN_JIT = "${shenJit}/bin/shen-jit";
          PYTHONDONTWRITEBYTECODE = "1";
        };
    in {
      devShells = eachSystem (system: {
        default = nixpkgs.legacyPackages.${system}.mkShell (environment system);
        research = let env = environment system; in nixpkgs.legacyPackages.${system}.mkShell (env // {
          nativeBuildInputs = env.nativeBuildInputs ++ [ capcov.packages.${system}.bifrost nixpkgs.legacyPackages.${system}.git ];
          PYTHONPATH = "${capcov}/src";
          BIFROST_SHEN_GO = "${capcov.packages.${system}.shen-go}/bin/shen";
        });
      });
      # tool/solo5: the pinned Solo5 source on every system; the tender, the toolchain
      # wrappers and the unikernel images on Linux.
      packages = eachSystem (system: import ./tool/solo5/nix { pkgs = nixpkgs.legacyPackages.${system}; });
      checks = eachSystem (system: {
        generated-applications = let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (p: [ p.cython ]);
          shen = pkgs.callPackage ./nix/shen-jit.nix { };
        in pkgs.runCommand "generated-applications" {
          nativeBuildInputs = [ python pkgs.clang shen ];
          PYTHONDONTWRITEBYTECODE = "1";
          SHEN_JIT = "${shen}/bin/shen-jit";
        } ''
          cd ${self}
          python3 example/qualify.py
          touch "$out"
        '';
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
        sim-tool = nixpkgs.legacyPackages.${system}.runCommand "sim-tool" (environment system) ''
          cd ${self}
          python3 -m tool check kernel sim accepted_job gossip
          touch "$out"
        '';
        # A gate of its own rather than one more name on the line above: the check takes
        # about six minutes, and other targets are being added next to it.
        workflow = nixpkgs.legacyPackages.${system}.runCommand "workflow" (environment system) ''
          cd ${self}
          python3 -m tool check workflow
          touch "$out"
        '';
        # The effect shim of tool/solo5 as a host process, against the simulator. The
        # unikernels themselves need Linux and a tap device: see tool/solo5/README.md.
        solo5-host = nixpkgs.legacyPackages.${system}.runCommand "solo5-host" (environment system) ''
          cd ${self}
          python3 -m tool check solo5-host
          touch "$out"
        '';
        # Three Raft servers on that shim, as three host processes with the harness as the
        # network between them: tool/solo5/README.md, "A cluster". Timers run on the
        # machine's clock, so this gate depends on the machine not being starved.
        solo5-cluster-host = nixpkgs.legacyPackages.${system}.runCommand "solo5-cluster-host" (environment system) ''
          cd ${self}
          python3 -m tool check solo5-cluster-host
          touch "$out"
        '';
        # The Raft target, as two checks so that each stays under ten minutes. TLC on the
        # published raft.tla is not one of them: see targets/raft/README.md, section 10.
        raft = nixpkgs.legacyPackages.${system}.runCommand "raft" (environment system) ''
          cd ${self}
          python3 -m tool check raft
          touch "$out"
        '';
        raft-mutants = nixpkgs.legacyPackages.${system}.runCommand "raft-mutants" (environment system) ''
          cd ${self}
          python3 -m targets.raft.check mutants
          touch "$out"
        '';
        native-replay = nixpkgs.legacyPackages.${system}.runCommand "native-replay" (environment system) ''
          python3 ${self}/logic/check.py
          python3 ${self}/native/check_accepted_job.py
          touch "$out"
        '';
      });
    };
}
