# services.shen-node.<name>: one compiled node under tool/host as the systemd unit shen-node-<name>.
# Its event log and durable file are /var/lib/shen-node/<name>/{events.log,disk.log}.
{ config, lib, ... }:
let
  cfg = config.services.shen-node;
  inherit (lib) mkOption types;
  listen = c: port: "${c.listenAddress}:${toString port}";
  peers = c:
    if c.peers == [ ] then [ (listen c c.peerPort) ]
    else lib.imap0 (n: peer: if n == c.node then listen c c.peerPort else peer) c.peers;
in
{
  options.services.shen-node = mkOption {
    default = { };
    description = "Compiled Shen nodes, each run by tool/host as shen-node-<name>.service.";
    type = types.attrsOf (types.submodule {
      options = {
        package = mkOption {
          type = types.package;
          description = "A node package of this flake: bin/node-host and share/node.";
        };
        node = mkOption {
          type = types.ints.unsigned;
          default = 0;
          description = "This node's number among the nodes the package was packed for.";
        };
        clientPort = mkOption {
          type = types.port;
          description = "TCP port for requests and outputs, one JSON value per line.";
        };
        peerPort = mkOption {
          type = types.port;
          description = "TCP port on which this node receives what other nodes send.";
        };
        peers = mkOption {
          type = types.listOf types.str;
          default = [ ];
          example = [ "node0:7100" "node1:7100" ];
          description = ''
            Every node's peer address in node order. This node's own entry is replaced by
            where it listens. Leave empty for a single node.
          '';
        };
        listenAddress = mkOption {
          type = types.str;
          default = "0.0.0.0";
          description = "Address both ports are bound to.";
        };
        openFirewall = mkOption {
          type = types.bool;
          default = false;
          description = "Open both ports in the firewall.";
        };
        autoStart = mkOption {
          type = types.bool;
          default = true;
          description = "Start the node at boot.";
        };
        environment = mkOption {
          type = types.attrsOf types.str;
          default = { };
          example = { HOST_CRASH = "synced:1"; };
          description = "Environment of the host process (HOST_CRASH, HOST_FULLFSYNC).";
        };
      };
    });
  };

  config = lib.mkIf (cfg != { }) {
    users.users.shen-node = { isSystemUser = true; group = "shen-node"; };
    users.groups.shen-node = { };
    networking.firewall.allowedTCPPorts = lib.concatMap
      (c: lib.optionals c.openFirewall [ c.clientPort c.peerPort ])
      (lib.attrValues cfg);
    systemd.services = lib.mapAttrs'
      (name: c: lib.nameValuePair "shen-node-${name}" {
        description = "Shen node ${name}, node ${toString c.node}";
        wantedBy = lib.optional c.autoStart "multi-user.target";
        after = [ "network.target" ];
        inherit (c) environment;
        # The host has no shutdown: every stop is a crash and every start delivers restart.
        # However often it dies, it is started again.
        startLimitIntervalSec = 0;
        serviceConfig = {
          ExecStart = lib.escapeShellArgs ([
            "${c.package}/bin/node-host"
            (toString c.node)
            "/var/lib/shen-node/${name}"
            (listen c c.clientPort)
          ] ++ peers c);
          StateDirectory = "shen-node/${name}";
          User = "shen-node";
          Group = "shen-node";
          Restart = "always";
          RestartSec = "100ms";
        };
      })
      cfg;
  };
}
