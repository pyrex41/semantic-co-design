# Solo5 and the unikernel images of tool/solo5.
#
#   flake.nix:   packages.<system> = import ./tool/solo5/nix { pkgs = ...; }
#   pinned.nix:  the same with the nixpkgs revision of flake.lock, for a machine where
#                the flake's other inputs cannot be fetched (a build container)
#
# Solo5 is the commit ~/projects/fenrir pins (v0.9.3, 38e0c348) and the build recipe is
# that flake's `mkSolo5`. nixpkgs' own solo5 (0.10.1) is in the binary cache for
# x86_64-linux only: on aarch64-linux it does not evaluate (its install phase needs
# syslinux), so there is no cached Solo5 for the machine this was developed on and the
# tender is built from source. That build is a few seconds of C; its compiler, binutils
# and libseccomp come from the cache.
{ pkgs }:
let
  lib = pkgs.lib;
  fs = lib.fileset;

  # The GitHub archive of the commit. (Fenrir locks the same commit as a git fetch,
  # whose NAR hash differs from the archive's.)
  solo5-src = pkgs.fetchFromGitHub {
    owner = "Solo5";
    repo = "solo5";
    rev = "38e0c348be231a2a3c59510a5caabf562792679f";
    hash = "sha256-KaJDpiX+S4LhVKWfE4qz4zUfA3oR+hl6BRDcXTu4CWc=";
  };

  # The guest compiler is the unwrapped gcc with the unwrapped binutils: a guest is
  # freestanding and must not pick up the nixpkgs wrapper's hardening flags or glibc.
  target-cc = pkgs.writeShellScript "solo5-target-cc" ''
    exec ${pkgs.stdenv.cc.cc}/bin/gcc -B${pkgs.binutils-unwrapped}/bin/ "$@"
  '';

  solo5 = pkgs.stdenv.mkDerivation {
    pname = "solo5";
    version = "0.9.3";
    src = solo5-src;
    nativeBuildInputs = [ pkgs.pkg-config ];
    buildInputs = [ pkgs.libseccomp ];
    # Host tools as position-independent executables; otherwise configure.sh pins the
    # spt tender at 1 GB.
    env.NIX_CFLAGS_COMPILE = "-fPIE -Wno-error";
    env.NIX_CFLAGS_LINK = "-pie";
    configurePhase = ''
      printf '#define SOLO5_VERSION "v0.9.3"\n' > include/version.h.distrib
      TARGET_CC=${target-cc} \
      TARGET_LD=${pkgs.binutils-unwrapped}/bin/ld \
      TARGET_OBJCOPY=${pkgs.binutils-unwrapped}/bin/objcopy \
        sh configure.sh --prefix=$out
    '';
    buildPhase = "make";
    installPhase = ''
      make install
      for w in $out/bin/*-solo5-none-static-*; do
        sed -i '2i PATH=${pkgs.coreutils}/bin:${pkgs.findutils}/bin:$PATH' $w
      done
      printf '%s\n' ${solo5-src.rev} > $out/SOLO5_COMMIT
    '';
    dontStrip = true; # the guest bindings in lib/ are linked into every image
  };

  # What the images are built from and what the runner executes: the tool and the
  # specializer's backend, nothing else of the repository.
  source = fs.toSource {
    root = ../../..;
    fileset = fs.difference (fs.unions [ ../../../tool ../../../specializer ])
      (fs.fileFilter (file: file.hasExt "md" || file.hasExt "nix" || file.hasExt "pyc") ../../..);
  };

  # Every image of tool/solo5/scenarios.py: NAME.spt for the spt tender, NAME.hvt for
  # the images listed there as HVT, and NAME.host, the same driver as a Linux process.
  images = pkgs.stdenv.mkDerivation {
    name = "sim-tool-solo5-images";
    src = source;
    nativeBuildInputs = [ pkgs.python3 solo5 ];
    PYTHONDONTWRITEBYTECODE = "1";
    dontConfigure = true;
    dontStrip = true;
    dontPatchELF = true;
    dontFixup = true;
    buildPhase = ''
      # The header the host-process driver is compiled against is the pinned one.
      cmp tool/solo5/include/solo5.h ${solo5-src}/include/solo5.h
      python3 -m tool.solo5.emit --all build
      guest=$(basename ${solo5}/bin/*-solo5-none-static-cc)
      flags="-std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter -Wno-unused-variable"
      mkdir -p $out
      $guest -c -std=c11 -O2 -fno-builtin -fno-tree-loop-distribute-patterns \
        tool/solo5/freestanding/lib.c -o build/lib.o
      hvt=" $(python3 -c 'from tool.solo5.scenarios import HVT; print(" ".join(HVT))') "
      for dir in build/*/; do
        name=$(basename $dir)
        solo5-elftool gen-manifest $dir/manifest.json $dir/manifest.c
        $guest -c $flags -I $dir $dir/driver.c -o $dir/driver.o
        $guest -c $flags -Wno-unused-function -I tool/solo5/freestanding $dir/node.c -o $dir/node.o
        $guest -c $dir/manifest.c -o $dir/manifest.o
        objects="$dir/driver.o $dir/node.o $dir/manifest.o build/lib.o"
        $guest -z solo5-abi=spt $objects -o $out/$name.spt
        case "$hvt" in *" $name "*) $guest -z solo5-abi=hvt $objects -o $out/$name.hvt ;; esac
        $CC $flags -I tool/solo5/include -I $dir $dir/driver.c tool/solo5/host.c $dir/node.c \
          -o $out/$name.host
      done
      cp build/node.c.sha256.json build/driver.c.sha256.json $out/
      for image in $out/*.spt $out/*.hvt; do
        printf '%s %s %s\n' "$(basename $image)" "$(solo5-elftool query-abi $image | tr -d ' \n')" \
          "$(solo5-elftool query-manifest $image | tr -d ' \n')"
      done > $out/images.txt
      printf '%s\n' ${solo5-src.rev} > $out/SOLO5_COMMIT
      # The unikernels only. A host process links against $out's own library path, so
      # its bytes depend on where the build writes; hashing it here would make this
      # derivation fail `nix-build --check`. The runner hashes the host processes.
      (cd $out && sha256sum *.spt *.hvt > sha256.txt)
    '';
    dontInstall = true;
  };

  # Runs every scenario against the unikernels under solo5-spt and against the Linux
  # host processes, and writes the records. Needs root, /dev/net/tun and CAP_NET_ADMIN:
  # the node's network device is a tap interface.
  run = pkgs.writeShellScriptBin "sim-tool-solo5-run" ''
    set -eu
    out=''${1:?usage: sim-tool-solo5-run OUTPUT-DIRECTORY [SCENARIO ...]}
    shift
    export PATH=${lib.makeBinPath [ pkgs.python3 pkgs.iproute2 pkgs.coreutils solo5 ]}:$PATH
    export PYTHONDONTWRITEBYTECODE=1
    ip link show tap0 >/dev/null 2>&1 || ip tuntap add tap0 mode tap
    ip link set dev tap0 up
    cd ${source}
    python3 -m tool.solo5.run spt ${images} "$out/spt.json" "$@"
    python3 -m tool.solo5.run host ${images} "$out/host.json" "$@"
    cp ${images}/images.txt ${images}/node.c.sha256.json ${images}/driver.c.sha256.json \
      ${images}/SOLO5_COMMIT ${images}/sha256.txt "$out/"
    echo ${images} > "$out/images-path.txt"
    # What ran: the tender binary (its build is not bit-reproducible, so the hash names
    # this build of it and no other) and the harness sources.
    solo5-spt --version > "$out/tender.txt" 2>&1 || true
    sha256sum "$(command -v solo5-spt)" >> "$out/tender.txt"
    (cd ${images} && sha256sum *.host) > "$out/host.sha256.txt"
    sha256sum tool/solo5/run.py tool/solo5/scenarios.py tool/solo5/mutants.py tool/solo5/emit.py \
      tool/solo5/driver.c tool/solo5/host.c > "$out/sources.sha256"
    uname -srm > "$out/uname.txt"
    # The hvt image: boot it for a few seconds if this machine has a hypervisor for it.
    # Status 124 means it was still running when the time was up.
    disk=$(mktemp)
    head -c 4096 /dev/zero > "$disk"
    status=0
    timeout 5 solo5-hvt --mem=16 --block:disk="$disk" --net:net=tap0 ${images}/accepted_job.hvt \
      > "$out/hvt.txt" 2>&1 || status=$?
    echo "status $status" >> "$out/hvt.txt"
    rm -f "$disk"
  '';
in
{
  inherit solo5-src;
} // lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
  inherit solo5;
  solo5-images = images;
  solo5-run = run;
}
