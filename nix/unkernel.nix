{ stdenvNoCC, fetchurl }:

# The published npm package, unpacked and otherwise untouched: the kernel
# (dist/unkernel.wasm, base system included) and its JavaScript host.
# experiments/unkernel_probe wraps the host from outside; nothing here is patched.
stdenvNoCC.mkDerivation rec {
  pname = "unkernel";
  version = "0.2.0-261124-75694e2";
  src = fetchurl {
    url = "https://registry.npmjs.org/unkernel/-/unkernel-${version}.tgz";
    hash = "sha256-GxMTBQ/VwahhTkNRkSPo/tl5dG9WjdQmeFsqMFRyv/Q=";
  };
  dontBuild = true;
  dontFixup = true;
  installPhase = ''
    mkdir -p $out
    cp -R . $out/
  '';
}
