flake:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.hrworksWorker;
  command = pkgs.writeShellApplication {
    name = "hrworks-worker";
    text = ''
      exec ${lib.getExe cfg.package} ${
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
    environment.systemPackages = [ command ];
  };
}
