{
  description = "HR WORKS employee CLI, Python library and Home Assistant browser worker";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      packages = forAllSystems (
        system:
        let
          hrworks = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix { };
        in
        {
          inherit hrworks;
          default = hrworks;
          worker = hrworks.overrideAttrs (old: {
            meta = old.meta // {
              mainProgram = "hrworks-worker";
            };
          });
        }
      );
      apps = forAllSystems (system: {
        default = self.apps.${system}.hrworks;
        hrworks = {
          type = "app";
          program = "${self.packages.${system}.hrworks}/bin/hrworks";
        };
        worker = {
          type = "app";
          program = "${self.packages.${system}.worker}/bin/hrworks-worker";
        };
      });
      nixosModules = {
        default = self.nixosModules.worker;
        worker = import ./nix/module.nix self;
        cli = import ./nix/cli-module.nix self;
      };
      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
