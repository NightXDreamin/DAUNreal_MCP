// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "Containers/Ticker.h"
#include "HAL/CriticalSection.h"
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

/** Lifecycle state of an async script job (mirrors DA_MCP_STATE in server/da_async.py). */
enum class EDaMCPJobState
{
	Running,
	Done,
	Error,
	Cancelled,
};

/** What kind of work a job performs. */
enum class EDaMCPJobKind
{
	/** Python generator driven by FTSTicker (existing async mode). */
	Python,
	/** Asset import via AssetTools/Interchange, executed from the tick. */
	Import,
	/** Blueprint compilation, executed from the tick. */
	Compile,
};

/**
 * One async job. Two flavours:
 *  - Python: drives the transformed generator (server/da_async.py) on the game
 *    thread via FTSTicker — one time-budgeted step per tick.
 *  - Import/Compile: heavy editor operations that CANNOT run inside the request
 *    callback stack (game thread inside AsyncTask(GameThread) → TaskGraph
 *    recursion / FlushRenderingCommands deadlock). They are executed from the
 *    normal FTSTicker tick instead, which is not a TaskGraph task context, so
 *    TaskGraph waits and render flushes complete normally.
 */
struct FDaMCPJob
{
	int32 JobId = 0;
	EDaMCPJobState State = EDaMCPJobState::Running;
	EDaMCPJobKind Kind = EDaMCPJobKind::Python;
	// --- Python flavour ---
	FString StepCode;     // executed each tick until the job finishes
	FString CancelCode;   // closes the generator (_da_gen.close())
	// --- Import flavour ---
	TArray<FString> ImportFilenames;
	TArray<FString> ImportDestinations;
	// --- Compile flavour ---
	TArray<FString> CompilePaths;
	// --- shared ---
	TArray<FString> NativeResults;   // per-item "ok"/"error: ..." (import/compile)
	FThreadSafeBool bCancelRequested;
	int32 SlicesDone = 0;
	FString Error;
	FString Output;   // accumulated non-status output (prints) of the job
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
 *
 * Request protocol (newline-delimited JSON):
 *   sync   : {"id":N, "code":"...", "token":"..."}       (backwards-compatible)
 *   async  : {"id":N, "action":"execute", "mode":"async",
 *             "setup_code":"...", "step_code":"...", "code":"...", "token":"..."}
 *   poll   : {"id":N, "action":"poll", "job_id":M, "token":"..."}
 *   cancel : {"id":N, "action":"cancel", "job_id":M, "token":"..."}
 *
 * ``token`` is optional when auth is disabled (token file write failed). The
 * bridge writes its auth token to <Saved>/DAUnrealMCP/endpoint.json on start.
 */
class FDAUnrealMCPBridge : public FRunnable
{
public:
	FDAUnrealMCPBridge() = default;
	virtual ~FDAUnrealMCPBridge();

	bool Start(int32 InPort);
	void Shutdown();

	/** Current listening port (last value passed to Start). */
	int32 GetPort() const { return Port; }

	/** True if an auth token is active. */
	bool HasAuthToken() const { return !AuthToken.IsEmpty(); }

	/** True while the worker thread + listener are up and accepting connections. */
	bool IsRunning() const { return (bool)bRunning && Thread != nullptr && ListenerSocket != nullptr; }

	// FRunnable
	virtual uint32 Run() override;
	virtual void Exit() override {}
	virtual void Stop() override { bStopping = true; }

private:
	bool ProcessConnection(FSocket* ClientSocket);
	void ClearActiveSocket();
	TSharedPtr<FDaMCPExecResult> ExecuteOnGameThread(const FString& Code);
	/** Executes Python on the game thread inside one FScopedTransaction. MUST be
	 *  called on the game thread (AsyncTask'd or from the ticker). */
	FDaMCPExecResult ExecuteInTransaction(const FString& Code);

	// --- async jobs ---
	int32 SubmitAsyncJob(const FString& SetupCode, const FString& StepCode, FString& OutError);
	int32 SubmitNativeJob(EDaMCPJobKind Kind, const TSharedPtr<FJsonObject>& ReqObj, FString& OutError);
	/** Execute one import/compile job body. Called from the ticker. Returns true
	 *  when the job is finished (result in NativeResults/Error). */
	bool RunNativeJob(FDaMCPJob& Job);
	/** True if any job is currently Running. */
	bool HasRunningJob();
	bool PollJob(int32 JobId, FString& OutStatus, int32& OutSlicesDone, FString& OutError, FString& OutOutput);
	void RequestCancel(int32 JobId);
	bool OnGameThreadTick(float DeltaTime);
	void RegisterTicker();
	void UnregisterTicker();
	void CleanupFinishedJobs();
	static void ParseState(const FString& Log, FDaMCPJob& Job);

	// --- security / audit ---
	bool IsAuthorized(const TSharedPtr<FJsonObject>& ReqObj) const;
	void LogHistory(const FString& Mode, const FString& Code, bool bOk, const FString& Error);

	FSocket* ListenerSocket = nullptr;
	FSocket* ActiveClientSocket = nullptr;
	FCriticalSection SocketLock;
	FRunnableThread* Thread = nullptr;
	FThreadSafeBool bRunning;
	FThreadSafeBool bStopping;
	int32 Port = 8765;
	FString AuthToken;  // empty => auth disabled (token file write failed)

	// --- async state ---
	FCriticalSection JobLock;
	TMap<int32, TSharedPtr<FDaMCPJob>> Jobs;
	int32 NextJobId = 1;
	FTSTicker::FDelegateHandle TickerHandle;
};
