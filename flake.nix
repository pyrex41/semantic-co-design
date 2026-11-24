{
  description = "Specialize executable Shen and Maude behavior specifications to native code";

  inputs.capcov.url = "github:pyrex41/capcov/23f7841ca7369bf793c633e682fb00c77afc644c";
  inputs.nixpkgs.follows = "capcov/nixpkgs";

  outputs = { self, nixpkgs, capcov }:
    let
      eachSystem = nixpkgs.lib.genAttrs [ "aarch64-darwin" "aarch64-linux" "x86_64-linux" ];
      environment = system:
        let pkgs = nixpkgs.legacyPackages.${system};
        in {
          nativeBuildInputs = [ pkgs.clang pkgs.python3 pkgs.maude capcov.packages.${system}.shen-go ];
          buildInputs = [ pkgs.sqlite ];
          SHEN_GO = "${capcov.packages.${system}.shen-go}/bin/shen";
          PYTHONDONTWRITEBYTECODE = "1";
        };
    in {
      devShells = eachSystem (system: {
        default = nixpkgs.legacyPackages.${system}.mkShell (environment system);
        research = let env = environment system; in nixpkgs.legacyPackages.${system}.mkShell (env // {
          nativeBuildInputs = env.nativeBuildInputs ++ [ capcov.packages.${system}.bifrost nixpkgs.legacyPackages.${system}.git ];
          PYTHONPATH = "${capcov}/src";
          BIFROST_SHEN_GO = env.SHEN_GO;
        });
      });
      checks = eachSystem (system: {
        native-replay = nixpkgs.legacyPackages.${system}.runCommand "native-replay" (environment system) ''
          python3 ${self}/logic/check.py
          python3 ${self}/native/check_accepted_job.py
          touch "$out"
        '';
      });
    };
}
