flake:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.hrworks;
in
{
  options.programs.hrworks = {
    enable = lib.mkEnableOption "the HR WORKS employee CLI";
    package = lib.mkOption {
      type = lib.types.package;
      default = flake.packages.${pkgs.stdenv.hostPlatform.system}.hrworks;
      description = "HR WORKS employee CLI package.";
    };
  };
  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
  };
}
