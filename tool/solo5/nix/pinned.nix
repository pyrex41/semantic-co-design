# The packages of ./default.nix with the nixpkgs revision that flake.lock pins, without
# evaluating the flake: a build container can fetch nixpkgs but not necessarily the
# flake's other inputs.
#
#   nix-build tool/solo5/nix/pinned.nix -A solo5-run
let
  lock = builtins.fromJSON (builtins.readFile ../../../flake.lock);
  node = lock.nodes.nixpkgs.locked;
  nixpkgs = builtins.fetchTarball {
    url = "https://github.com/${node.owner}/${node.repo}/archive/${node.rev}.tar.gz";
    sha256 = node.narHash;
  };
in
import ./default.nix { pkgs = import nixpkgs { config = { }; overlays = [ ]; }; }
