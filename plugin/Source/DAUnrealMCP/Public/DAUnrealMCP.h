// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

class FDAUnrealMCPBridge;

class FDAUnrealMCPModule : public IModuleInterface
{
public:
	// IModuleInterface
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

	/** Bridge accessor for the in-editor UI (menu / panel). */
	FDAUnrealMCPBridge* GetBridge() const { return Bridge; }

	/** Start listening on (possibly new) port; persists the port to config. Returns success. */
	bool RequestStart(int32 InPort);
	/** Stop listening. Returns success (true if already stopped). */
	bool RequestStop();
	/** Convenience: stop then start on InPort (or current port if 0). */
	bool RequestRestart(int32 InPort);

	/** Read the configured port (DefaultEngine.ini [DAUnrealMCP] Port). */
	static int32 GetConfiguredPort();
	/** Persist Port to DefaultEngine.ini [DAUnrealMCP]. */
	static void SetConfiguredPort(int32 InPort);

	/** Register the Window > Layout menu entry (called once from StartupModule). */
	void RegisterMenuEntry();

private:
	FDAUnrealMCPBridge* Bridge = nullptr;
};
