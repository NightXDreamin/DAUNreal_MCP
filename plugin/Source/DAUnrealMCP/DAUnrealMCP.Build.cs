// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;

public class DAUnrealMCP : ModuleRules
{
	public DAUnrealMCP(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"Slate",
			"SlateCore",
			"InputCore",
			"ToolMenus"
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Networking",
			"Sockets",
			"Json",
			"JsonUtilities",
			"PythonScriptPlugin",
			"UnrealEd",
			"UMG",
			"UMGEditor",
			"AssetTools",
			"Kismet"
		});
	}
}
