// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUnrealMCP.h"

#include "DAUnrealMCPBridge.h"
#include "Misc/ConfigCacheIni.h"
#include "Misc/Paths.h"
#include "HAL/PlatformFileManager.h"
#include "ToolMenus.h"

#define LOCTEXT_NAMESPACE "FDAUnrealMCPModule"

static constexpr int32 DefaultPort = 8765;

void FDAUnrealMCPModule::StartupModule()
{
	Bridge = new FDAUnrealMCPBridge();
	RequestStart(GetConfiguredPort());

	// Register the Window menu entry when ToolMenus are ready.
	UToolMenus::RegisterStartupCallback(FSimpleMulticastDelegate::FDelegate::CreateRaw(this, &FDAUnrealMCPModule::RegisterMenuEntry));
}

void FDAUnrealMCPModule::ShutdownModule()
{
	UToolMenus::UnRegisterStartupCallback(this);
	UToolMenus::UnregisterOwner(this);

	if (Bridge)
	{
		Bridge->Shutdown();
		delete Bridge;
		Bridge = nullptr;
	}
}

int32 FDAUnrealMCPModule::GetConfiguredPort()
{
	int32 Port = DefaultPort;
	if (GConfig)
	{
		GConfig->GetInt(TEXT("DAUnrealMCP"), TEXT("Port"), Port, GEngineIni);
	}
	return Port;
}

void FDAUnrealMCPModule::SetConfiguredPort(int32 InPort)
{
	if (GConfig)
	{
		GConfig->SetInt(TEXT("DAUnrealMCP"), TEXT("Port"), InPort, GEngineIni);
		GConfig->Flush(false, GEngineIni);
	}
}

bool FDAUnrealMCPModule::RequestStart(int32 InPort)
{
	if (!Bridge)
	{
		Bridge = new FDAUnrealMCPBridge();
	}

	if (Bridge->IsRunning())
	{
		// Already running on the same port — nothing to do.
		if (InPort == Bridge->GetPort())
		{
			return true;
		}
		// Port changed: stop first (releases the old bind), then start on the new port.
		Bridge->Shutdown();
	}

	if (InPort > 0)
	{
		SetConfiguredPort(InPort);
	}
	else
	{
		InPort = GetConfiguredPort();
	}

	if (Bridge->Start(InPort))
	{
		UE_LOG(LogTemp, Log, TEXT("[DAUnrealMCP] Bridge listening on 127.0.0.1:%d"), InPort);
		return true;
	}

	UE_LOG(LogTemp, Warning, TEXT("[DAUnrealMCP] Failed to start bridge on 127.0.0.1:%d (port already in use?)"), InPort);
	return false;
}

bool FDAUnrealMCPModule::RequestStop()
{
	if (!Bridge || !Bridge->IsRunning())
	{
		return true;
	}
	Bridge->Shutdown();
	UE_LOG(LogTemp, Log, TEXT("[DAUnrealMCP] Bridge stopped."));
	return true;
}

bool FDAUnrealMCPModule::RequestRestart(int32 InPort)
{
	if (InPort <= 0)
	{
		InPort = GetConfiguredPort();
	}
	RequestStop();
	return RequestStart(InPort);
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FDAUnrealMCPModule, DAUnrealMCP)
