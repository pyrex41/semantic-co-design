# NixOS machine tests: the packaged nodes on virtual machines with a real kernel, network and disk.
# The scripts are nix/vm.py and nix/vm-mutant.py; tool/README.md, section 5, says what they show.
{ pkgs, packages }:
let
  inherit (packages) runtime conform;
  job = packages.node { example = "accepted_job"; };
  mutant = packages.node { example = "accepted_job"; mutant = "reply-before-ack"; };
  heartbeat = packages.node { example = "heartbeat"; nodes = 2; };
  script = file: names: builtins.replaceStrings
    (map (name: "@${name}@") (builtins.attrNames names))
    (map toString (builtins.attrValues names))
    (builtins.readFile file);
  client = { environment.systemPackages = [ runtime ]; };
  machine = node: deployed: {
    imports = [ ./module.nix ];
    services.shen-node = {
      # Each machine has its own AcceptedJob node: one node, its own disk.
      job = { package = deployed; clientPort = 7000; peerPort = 7100; openFirewall = true; };
      # The two heartbeat nodes are one system; the test starts them when it gets there.
      heartbeat = {
        package = heartbeat;
        inherit node;
        clientPort = 7001;
        peerPort = 7101;
        peers = [ "node0:7101" "node1:7101" ];
        openFirewall = true;
        autoStart = false;
      };
    };
    environment.systemPackages = [ pkgs.iproute2 pkgs.iptables ];
    boot.kernelModules = [ "sch_netem" ];
  };
in
{
  real-vm = pkgs.testers.runNixOSTest {
    name = "real-vm";
    nodes = { inherit client; node0 = machine 0 job; node1 = machine 1 job; };
    testScript = script ./vm.py {
      run = "${runtime}/bin/node-run";
      conform = "${conform}/bin/node-conform";
      job = "${job}/share/node";
      heartbeat = "${heartbeat}/share/node";
    };
  };
  # Teeth: the same deployment with one edit in the specification must fail conformance.
  real-vm-mutant = pkgs.testers.runNixOSTest {
    name = "real-vm-mutant";
    nodes = { inherit client; node0 = machine 0 mutant; };
    testScript = script ./vm-mutant.py {
      run = "${runtime}/bin/node-run";
      conform = "${conform}/bin/node-conform";
      spec = "${job}/share/node";
      job = "${mutant}/share/node";
    };
  };
}
