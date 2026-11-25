{ lib, stdenv, buildPythonPackage, fetchurl, autoPatchelfHook }:

# Hegel's official Python frontend (PyPI: hegeltest), which bundles the libhegel engine.
# The published cp314 wheels are fetched by hash; nothing is compiled here.
let
  version = "0.5.2";
  wheels = {
    aarch64-darwin = {
      path = "a2/20/2499df6326be2f55275d4bb9ea85463cd2dbb1b18e68f71397bc972518d8";
      platform = "macosx_11_0_arm64";
      sha256 = "45d82c8d580b9b2cadea426e44be4f525935ac8e2437c53765b041596c19117f";
    };
    aarch64-linux = {
      path = "7f/32/0b2b810f3b118c618a6ba4aa6bc961e35f9cd4bbb86e5cda9934d909263d";
      platform = "manylinux2014_aarch64.manylinux_2_17_aarch64";
      sha256 = "22f4e5165662d761f77dc41eb72b98ffd55f88226c1fcec3a0a93b54919514b0";
    };
    x86_64-linux = {
      path = "ca/49/a10f0a95165b4b3c00e2e60672077bb19ee89bb8fe88f454be125c02a6a6";
      platform = "manylinux2014_x86_64.manylinux_2_17_x86_64";
      sha256 = "a8a385fe9ddca68f35370361d4229d62a9aad781e39759dd8fd79489b5fff8be";
    };
  };
  wheel = wheels.${stdenv.hostPlatform.system};
in
buildPythonPackage {
  pname = "hegeltest";
  inherit version;
  format = "wheel";
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/${wheel.path}/hegeltest-${version}-cp314-cp314-${wheel.platform}.whl";
    inherit (wheel) sha256;
  };
  nativeBuildInputs = lib.optionals stdenv.hostPlatform.isLinux [ autoPatchelfHook ];
  buildInputs = lib.optionals stdenv.hostPlatform.isLinux [ stdenv.cc.cc.lib ];
  pythonImportsCheck = [ "hegel" ];
}
