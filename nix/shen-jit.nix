{ stdenvNoCC, fetchurl }:

let
  version = "0.11";
  releases = {
    aarch64-darwin = {
      platform = "macos-arm64";
      sha256 = "77da4e320f4cbd78df53b79d775ef279309ff85308fe8ed3d6356bf050324a06";
    };
    aarch64-linux = {
      platform = "linux-arm64-static";
      sha256 = "cc821d6c352bbe2002b19300b6c496c15520bd6a05be2528e4e3642911e02752";
    };
    x86_64-linux = {
      platform = "linux-x86_64-static";
      sha256 = "86001156f4106f7dcc68ad961593f3b0699c8d06e04a11f457c5586a27002da0";
    };
  };
  release = releases.${stdenvNoCC.hostPlatform.system};
in
stdenvNoCC.mkDerivation {
  pname = "shen-jit";
  inherit version;
  src = fetchurl {
    url = "https://github.com/tizoc/shen-jit/releases/download/v${version}/shen-jit-${version}-${release.platform}.tar.gz";
    inherit (release) sha256;
  };
  dontBuild = true;
  # The upstream executable contains an appended Shen image.
  dontFixup = true;
  installPhase = ''
    mkdir -p "$out/bin" "$out/share/licenses/shen-jit"
    cp shen-jit "$out/bin/"
    cp -R lib "$out/"
    cp LICENSE*.txt "$out/share/licenses/shen-jit/"
  '';
}
