// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUnrealMCP.h"
#include "DAUnrealMCPSlate.h"

#include "ToolMenus.h"

#define LOCTEXT_NAMESPACE "DAUnrealMCPMenu"

/**
 * Registers the DAUnreal MCP entry under Window > Layout (not a new top-level
 * menu bar). The parent menu is created by UE's MainFrame module; we only
 * extend the existing section so the entry appears alongside Load/Save Layout.
 */
void FDAUnrealMCPModule::RegisterMenuEntry()
{
	UToolMenus* ToolMenus = UToolMenus::Get();
	if (!ToolMenus)
	{
		return;
	}

	// Extend the existing Window menu, Layout section — do NOT register a new
	// top-level menu (the user explicitly asked to avoid a separate menu bar).
	// NOTE: the rendered Window menu is "LevelEditor.MainMenu.Window"; the
	// "MainFrame.MainMenu.Window" name is only its parent (never displayed).
	UToolMenu* WindowMenu = ToolMenus->ExtendMenu("LevelEditor.MainMenu.Window");
	if (!WindowMenu)
	{
		return;
	}

	FToolMenuSection& Section = WindowMenu->FindOrAddSection("WindowLayout");

	Section.AddMenuEntry(
		"DAUnrealMCP",
		LOCTEXT("DAUnrealMCPLabel", "DAUnreal MCP Bridge…"),
		LOCTEXT("DAUnrealMCPTooltip", "Open the DAUnreal MCP bridge control panel (start/stop listening, change port)."),
		FSlateIcon(),
		FUIAction(FExecuteAction::CreateLambda([]()
		{
			FDAUnrealMCPSlate::ShowPanel();
		}))
	);
}

#undef LOCTEXT_NAMESPACE
