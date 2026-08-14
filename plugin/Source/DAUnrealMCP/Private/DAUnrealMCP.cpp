// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUnrealMCP.h"

#include "DAUnrealMCPBridge.h"
#include "Misc/ConfigCacheIni.h"

#define LOCTEXT_NAMESPACE "FDAUnrealMCPModule"

void FDAUnrealMCPModule::StartupModule()
{
	int32 Port = 8765;
	if (GConfig)
	{
		GConfig->GetInt(TEXT("DAUnrealMCP"), TEXT("Port"), Port, GEngineIni);
	}

	Bridge = new FDAUnrealMCPBridge();
	if (Bridge->Start(Port))
	{
		UE_LOG(LogTemp, Log, TEXT("[DAUnrealMCP] Bridge listening on 127.0.0.1:%d"), Port);
	}
	else
	{
		UE_LOG(LogTemp, Warning, TEXT("[DAUnrealMCP] Failed to start bridge on 127.0.0.1:%d (port already in use?)"), Port);
	}
}

void FDAUnrealMCPModule::ShutdownModule()
{
	if (Bridge)
	{
		Bridge->Shutdown();
		delete Bridge;
		Bridge = nullptr;
	}
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FDAUnrealMCPModule, DAUnrealMCP)
