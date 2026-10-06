flake:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.hrworksWorker;
  configuredPackage = pkgs.symlinkJoin {
    name = "hrworks-configured";
    paths = [ cfg.package ];
    postBuild = ''
      rm "$out/bin/hrworks"
      cat > "$out/bin/hrworks" <<'EOF'
      #!${pkgs.bash}/bin/bash
      if [[ "''${1:-}" == worker ]]; then
        shift
        exec ${cfg.package}/bin/hrworks worker ${
          lib.escapeShellArgs (
            [
              "--cdp-url"
              cfg.cdpUrl
            ]
            ++ lib.optionals (cfg.stateDirectory != null) [
              "--state-dir"
              cfg.stateDirectory
            ]
            ++ lib.optional cfg.enableWrites "--enable-writes"
          )
        } "$@"
      fi
      # The CLI starts its own `hrworks worker` from inside the package, bypassing
      # this wrapper; HRWORKS_CDP_URL makes it pass --cdp-url to that worker.
      if [[ -z "''${HRWORKS_CDP_URL:-}" ]]; then
        export HRWORKS_CDP_URL=${lib.escapeShellArg cfg.cdpUrl}
      fi
      exec ${cfg.package}/bin/hrworks "$@"
      EOF
      chmod 755 "$out/bin/hrworks"
    '';
  };
in
{
  options.programs.hrworksWorker = {
    enable = lib.mkEnableOption "the HR WORKS browser executable for SSH access";
    package = lib.mkOption {
      type = lib.types.package;
      default = flake.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "Pinned browser worker package.";
    };
    cdpUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://127.0.0.1:9222";
      description = "Chromium CDP endpoint on the SSH host.";
    };
    stateDirectory = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Session directory; null uses the SSH user's private state directory.";
    };
    enableWrites = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Permit submissions when also enabled in Home Assistant.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ configuredPackage ];
  };
}
