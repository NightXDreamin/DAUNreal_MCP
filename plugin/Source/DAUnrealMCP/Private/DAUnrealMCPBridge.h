// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "HAL/Runnable.h"
#include "HAL/RunnableThread.h"
#include "HAL/ThreadSafeBool.h"

class FSocket;

/** Result of a single Python execution dispatched to the game thread. */
struct FDaMCPExecResult
{
	bool bOk = false;
	FString Log;
	FString Result;
	FString Error;
};

/**
 * Local NDJSON-over-TCP bridge. Runs its own worker thread with a polling accept
 * loop (mirrors the proven Mochi/FMochiHttpServer pattern), serving ONE request
 * per connection: read a newline-delimited JSON request, execute the Python code
 * on the game thread, send one JSON response line, then close the socket.
 *
 * Python execution uses EPythonFileExecutionScope::Public so the namespace
 * (variables/imports) persists across requests regardless of the TCP connection
 * being short-lived.
 */
class FDAUnrealMCPBridge : public FRunnable
{
public:
	FDAUnrealMCPBridge() = default;
	virtual ~FDAUnrealMCPBridge();

	bool Start(int32 InPort);
	void Shutdown();

	// FRunnable
	virtual uint32 Run() override;
	virtual void Exit() override {}
	virtual void Stop() override { bStopping = true; }

private:
	bool ProcessConnection(FSocket* ClientSocket);
	TSharedPtr<FDaMCPExecResult> ExecuteOnGameThread(const FString& Code);

	FSocket* ListenerSocket = nullptr;
	FRunnableThread* Thread = nullptr;
	FThreadSafeBool bRunning;
	FThreadSafeBool bStopping;
	int32 Port = 8765;
};
