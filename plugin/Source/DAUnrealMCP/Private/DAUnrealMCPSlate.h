// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"

/**
 * In-editor panel for the DAUnreal MCP bridge.
 *
 * Mirrors DAMaya_MCP's in-DCC control panel, but implemented in Slate because
 * the bridge runs inside the C++ plugin (not an external process). Shows the
 * current listen state and lets the user start/stop the bridge or change the
 * port. Registered under Window > Layout in the main editor menu; NOT a new
 * top-level menu bar.
 */
namespace FDAUnrealMCPSlate
{
	/** Show (or raise) the panel window. Safe to call repeatedly. */
	void ShowPanel();

	/** Close the panel if it is open. */
	void ClosePanel();
}
