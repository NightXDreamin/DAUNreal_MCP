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

private:
	FDAUnrealMCPBridge* Bridge = nullptr;
};
