// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUnrealMCPBridge.h"

#include "Async/Async.h"
#include "Async/Future.h"
#include "Containers/StringConv.h"
#include "Dom/JsonObject.h"
#include "HAL/PlatformMemory.h"
#include "HAL/PlatformProcess.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "PythonScriptPlugin/Public/IPythonScriptPlugin.h"
#include "ScopedTransaction.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "SocketSubsystem.h"
#include "Sockets.h"

namespace
{
	constexpr int32 MaxLineLength = 16 * 1024 * 1024; // 16 MiB safety cap

	/** Read one newline-terminated UTF-8 line from a BLOCKING socket. Returns false on error/EOF. */
	bool ReadLine(FSocket* Socket, FString& OutLine)
	{
		OutLine.Reset();
		TArray<uint8> Bytes;
		Bytes.Reserve(1024);

		uint8 Ch = 0;
		int32 BytesRead = 0;
		while (true)
		{
			if (!Socket->Recv(&Ch, 1, BytesRead))
			{
				return false;
			}
			if (BytesRead == 0)
			{
				// Safety net: if the socket is somehow non-blocking, yield and retry.
				FPlatformProcess::Sleep(0.001f);
				continue;
			}
			if (Ch == '\n')
			{
				break;
			}
			if (Ch != '\r')
			{
				Bytes.Add(Ch);
				if (Bytes.Num() > MaxLineLength)
				{
					return false;
				}
			}
		}

		if (Bytes.Num() == 0)
		{
			OutLine = TEXT("");
			return true;
		}

		Bytes.Add(0); // null-terminate for UTF8_TO_TCHAR
		OutLine = UTF8_TO_TCHAR(reinterpret_cast<const char*>(Bytes.GetData()));
		return true;
	}

	/** Send a UTF-8 string followed by a newline on a BLOCKING socket. */
	bool SendLine(FSocket* Socket, const FString& Line)
	{
		FTCHARToUTF8 Converter(*Line);
		TArray<uint8> Buffer;
		const int32 Length = Converter.Length();
		Buffer.SetNumUninitialized(Length + 1);
		FMemory::Memcpy(Buffer.GetData(), Converter.Get(), Length);
		Buffer[Length] = '\n';

		int32 Sent = 0;
		while (Sent < Buffer.Num())
		{
			int32 BytesSent = 0;
			if (!Socket->Send(Buffer.GetData() + Sent, Buffer.Num() - Sent, BytesSent))
			{
				return false;
			}
			if (BytesSent <= 0)
			{
				return false;
			}
			Sent += BytesSent;
		}
		return true;
	}

	/** Serialize a JSON object to a single line (no whitespace), for NDJSON framing. */
	FString SerializeCondensed(const TSharedRef<FJsonObject>& Obj)
	{
		FString Out;
		TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
			TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Out);
		FJsonSerializer::Serialize(Obj, Writer);
		return Out;
	}

	/** Send a JSON error response. */
	void SendErrorResponse(FSocket* Socket, int32 Id, const FString& Message)
	{
		TSharedRef<FJsonObject> Resp = MakeShareable(new FJsonObject());
		Resp->SetNumberField(TEXT("id"), Id);
		Resp->SetBoolField(TEXT("ok"), false);
		Resp->SetStringField(TEXT("error"), Message);
		SendLine(Socket, SerializeCondensed(Resp));
	}
}

FDAUnrealMCPBridge::~FDAUnrealMCPBridge()
{
	Shutdown();
}

bool FDAUnrealMCPBridge::Start(int32 InPort)
{
	Port = InPort;
	bStopping = false;
	bRunning = false;

	ISocketSubsystem* SocketSubsystem = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
	if (!SocketSubsystem)
	{
		UE_LOG(LogTemp, Error, TEXT("[DAUnrealMCP] No socket subsystem"));
		return false;
	}

	ListenerSocket = SocketSubsystem->CreateSocket(NAME_Stream, TEXT("DAUnrealMCP"), false);
	if (!ListenerSocket)
	{
		UE_LOG(LogTemp, Error, TEXT("[DAUnrealMCP] Failed to create socket"));
		return false;
	}

	ListenerSocket->SetReuseAddr(true);
	ListenerSocket->SetNonBlocking(false);

	FIPv4Endpoint Endpoint(FIPv4Address::InternalLoopback, static_cast<uint16>(Port));
	if (!ListenerSocket->Bind(*Endpoint.ToInternetAddr()))
	{
		UE_LOG(LogTemp, Error, TEXT("[DAUnrealMCP] Failed to bind 127.0.0.1:%d"), Port);
		SocketSubsystem->DestroySocket(ListenerSocket);
		ListenerSocket = nullptr;
		return false;
	}

	if (!ListenerSocket->Listen(8))
	{
		UE_LOG(LogTemp, Error, TEXT("[DAUnrealMCP] Failed to listen on %d"), Port);
		SocketSubsystem->DestroySocket(ListenerSocket);
		ListenerSocket = nullptr;
		return false;
	}

	bRunning = true;
	Thread = FRunnableThread::Create(this, TEXT("DAUnrealMCPBridge"), 0, TPri_Normal);
	if (!Thread)
	{
		bRunning = false;
		SocketSubsystem->DestroySocket(ListenerSocket);
		ListenerSocket = nullptr;
		return false;
	}

	UE_LOG(LogTemp, Log, TEXT("[DAUnrealMCP] Bridge listening on 127.0.0.1:%d"), Port);
	return true;
}

void FDAUnrealMCPBridge::Shutdown()
{
	bStopping = true;
	bRunning = false;

	// Stop driving async jobs and drop them (editor is shutting down; the
	// generators live in the editor Python VM which dies with it).
	UnregisterTicker();
	{
		FScopeLock Lock(&JobLock);
		Jobs.Reset();
	}

	// Unblock a worker thread stuck in ReadLine() on the active client socket.
	{
		FScopeLock Lock(&SocketLock);
		if (ActiveClientSocket)
		{
			ActiveClientSocket->Close();
		}
	}

	// Join the worker BEFORE touching the listener socket, so we never destroy the
	// listener while the worker may still be inside HasPendingConnection/Accept.
	if (Thread)
	{
		Thread->WaitForCompletion();
		delete Thread;
		Thread = nullptr;
	}

	if (ListenerSocket)
	{
		ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ListenerSocket);
		ListenerSocket = nullptr;
	}
}

uint32 FDAUnrealMCPBridge::Run()
{
	while (bRunning && !bStopping)
	{
		bool bHasPendingConnection = false;
		if (ListenerSocket && ListenerSocket->HasPendingConnection(bHasPendingConnection) && bHasPendingConnection)
		{
			FSocket* ClientSocket = ListenerSocket->Accept(TEXT("DAUnrealMCPClient"));
			if (ClientSocket)
			{
				if (bStopping)
				{
					ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ClientSocket);
					break;
				}

				int32 BufferSize = 65536;
				ClientSocket->SetReceiveBufferSize(BufferSize, BufferSize);
				ClientSocket->SetSendBufferSize(BufferSize, BufferSize);
				ClientSocket->SetNonBlocking(false);

				ProcessConnection(ClientSocket);

				ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(ClientSocket);
			}
		}
		else
		{
			FPlatformProcess::Sleep(0.01f);
		}
	}
	return 0;
}

void FDAUnrealMCPBridge::ClearActiveSocket()
{
	FScopeLock Lock(&SocketLock);
	ActiveClientSocket = nullptr;
}

bool FDAUnrealMCPBridge::ProcessConnection(FSocket* ClientSocket)
{
	{
		FScopeLock Lock(&SocketLock);
		ActiveClientSocket = ClientSocket;
	}

	FString Line;
	if (!ReadLine(ClientSocket, Line))
	{
		ClearActiveSocket();
		return false;
	}

	TSharedPtr<FJsonObject> ReqObj;
	TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Line);
	if (!FJsonSerializer::Deserialize(Reader, ReqObj) || !ReqObj.IsValid())
	{
		SendErrorResponse(ClientSocket, 0, TEXT("invalid JSON request"));
		ClearActiveSocket();
		return true;
	}

	const int32 Id = ReqObj->GetIntegerField(TEXT("id"));
	const FString Action = ReqObj->HasField(TEXT("action")) ? ReqObj->GetStringField(TEXT("action")) : TEXT("execute");

	if (Action == TEXT("poll"))
	{
		const int32 JobId = ReqObj->GetIntegerField(TEXT("job_id"));
		FString Status;
		int32 SlicesDone = 0;
		FString Error;
		FString Output;
		if (!PollJob(JobId, Status, SlicesDone, Error, Output))
		{
			SendErrorResponse(ClientSocket, Id, TEXT("unknown job_id"));
			ClearActiveSocket();
			return true;
		}

		TSharedRef<FJsonObject> Resp = MakeShareable(new FJsonObject());
		Resp->SetNumberField(TEXT("id"), Id);
		Resp->SetBoolField(TEXT("ok"), true);
		Resp->SetNumberField(TEXT("job_id"), JobId);
		Resp->SetStringField(TEXT("status"), Status);
		Resp->SetNumberField(TEXT("slices_done"), SlicesDone);
		if (!Error.IsEmpty())
		{
			Resp->SetStringField(TEXT("error"), Error);
		}
		if (!Output.IsEmpty())
		{
			Resp->SetStringField(TEXT("output"), Output);
		}
		SendLine(ClientSocket, SerializeCondensed(Resp));
		ClearActiveSocket();
		return true;
	}

	if (Action == TEXT("cancel"))
	{
		const int32 JobId = ReqObj->GetIntegerField(TEXT("job_id"));
		RequestCancel(JobId);

		TSharedRef<FJsonObject> Resp = MakeShareable(new FJsonObject());
		Resp->SetNumberField(TEXT("id"), Id);
		Resp->SetBoolField(TEXT("ok"), true);
		Resp->SetNumberField(TEXT("job_id"), JobId);
		Resp->SetStringField(TEXT("status"), TEXT("cancelling"));
		SendLine(ClientSocket, SerializeCondensed(Resp));
		ClearActiveSocket();
		return true;
	}

	// --- execute ---
	const FString Mode = ReqObj->HasField(TEXT("mode")) ? ReqObj->GetStringField(TEXT("mode")) : TEXT("sync");

	if (Mode == TEXT("async"))
	{
		const FString SetupCode = ReqObj->GetStringField(TEXT("setup_code"));
		const FString StepCode = ReqObj->GetStringField(TEXT("step_code"));

		// Single concurrent job: reject while another job is still running.
		{
			FScopeLock Lock(&JobLock);
			for (const auto& KV : Jobs)
			{
				if (KV.Value->State == EDaMCPJobState::Running)
				{
					SendErrorResponse(ClientSocket, Id, TEXT("another async job is still running"));
					ClearActiveSocket();
					return true;
				}
			}
		}

		FString SetupError;
		const int32 JobId = SubmitAsyncJob(SetupCode, StepCode, SetupError);
		if (JobId < 0)
		{
			SendErrorResponse(ClientSocket, Id, SetupError.IsEmpty() ? TEXT("async submit failed") : SetupError);
			ClearActiveSocket();
			return true;
		}

		TSharedRef<FJsonObject> Resp = MakeShareable(new FJsonObject());
		Resp->SetNumberField(TEXT("id"), Id);
		Resp->SetBoolField(TEXT("ok"), true);
		Resp->SetNumberField(TEXT("job_id"), JobId);
		Resp->SetStringField(TEXT("status"), TEXT("running"));
		SendLine(ClientSocket, SerializeCondensed(Resp));
		ClearActiveSocket();
		return true;
	}

	// --- sync path (existing behaviour) ---
	const FString Code = ReqObj->GetStringField(TEXT("code"));

	TSharedPtr<FDaMCPExecResult> R = ExecuteOnGameThread(Code);
	if (!R.IsValid())
	{
		// Bridge is shutting down and the game thread asked us to abort.
		ClearActiveSocket();
		return false;
	}

	TSharedRef<FJsonObject> Resp = MakeShareable(new FJsonObject());
	Resp->SetNumberField(TEXT("id"), Id);
	Resp->SetBoolField(TEXT("ok"), R->bOk);
	if (!R->Log.IsEmpty())
	{
		Resp->SetStringField(TEXT("log"), R->Log);
	}

	if (R->bOk)
	{
		if (!R->Result.IsEmpty() && R->Result != TEXT("None"))
		{
			Resp->SetStringField(TEXT("result"), R->Result);
		}
	}
	else
	{
		FString Err = R->Error.IsEmpty() ? R->Result : R->Error;
		if (Err.IsEmpty())
		{
			Err = TEXT("unknown error");
		}
		Resp->SetStringField(TEXT("error"), Err);
	}

	const bool bSent = SendLine(ClientSocket, SerializeCondensed(Resp));
	ClearActiveSocket();
	return bSent;
}

TSharedPtr<FDaMCPExecResult> FDAUnrealMCPBridge::ExecuteOnGameThread(const FString& Code)
{
	TSharedPtr<TPromise<TSharedPtr<FDaMCPExecResult>>> Promise = MakeShareable(new TPromise<TSharedPtr<FDaMCPExecResult>>());
	TFuture<TSharedPtr<FDaMCPExecResult>> Future = Promise->GetFuture();

	FDAUnrealMCPBridge* Self = this;
	AsyncTask(ENamedThreads::GameThread, [Self, Promise, Code]()
	{
		TSharedPtr<FDaMCPExecResult> R = MakeShareable(new FDaMCPExecResult());
		*R = Self->ExecuteInTransaction(Code);
		Promise->SetValue(R);
	});

	// Poll instead of blocking forever, so Shutdown() on the game thread can never
	// deadlock against this worker thread waiting for the game thread to run the lambda.
	while (!Future.IsReady())
	{
		if (bStopping)
		{
			return nullptr;
		}
		FPlatformProcess::Sleep(0.002f);
	}
	return Future.Get();
}

FDaMCPExecResult FDAUnrealMCPBridge::ExecuteInTransaction(const FString& Code)
{
	FDaMCPExecResult R;
	{
		// Wrap execution in an undo transaction so AI-driven edits can be rolled
		// back with Ctrl+Z (mirrors DAMaya_MCP's undo chunk). Pure-query scripts
		// produce an empty transaction that UTransBuffer discards, so this is
		// harmless for reads. For async jobs each step is its own transaction,
		// so a cancelled job leaves already-applied steps undoable.
		FScopedTransaction Transaction(FText::FromString(TEXT("DAUnreal MCP Execute Python")));

		IPythonScriptPlugin* Py = IPythonScriptPlugin::Get();
		if (!Py || !Py->IsPythonAvailable())
		{
			R.bOk = false;
			R.Error = TEXT("PythonScriptPlugin is not available. Enable the 'Python Editor Script Plugin' and restart the editor.");
		}
		else
		{
			FPythonCommandEx Cmd;
			Cmd.Command = Code;
			Cmd.ExecutionMode = EPythonCommandExecutionMode::ExecuteFile;
			Cmd.FileExecutionScope = EPythonFileExecutionScope::Public;
			Cmd.Flags = EPythonCommandFlags::None;

			const bool bSuccess = Py->ExecPythonCommandEx(Cmd);

			R.bOk = bSuccess;
			R.Result = Cmd.CommandResult;

			FString LogStr;
			for (const FPythonLogOutputEntry& Entry : Cmd.LogOutput)
			{
				if (Entry.Output.IsEmpty())
				{
					continue;
				}
				if (!LogStr.IsEmpty())
				{
					LogStr += TEXT("\n");
				}
				LogStr += Entry.Output;
			}
			R.Log = LogStr;
		}
	}
	return R;
}

// --------------------------------------------------------------------------- //
// async jobs
// --------------------------------------------------------------------------- //

int32 FDAUnrealMCPBridge::SubmitAsyncJob(const FString& SetupCode, const FString& StepCode, FString& OutError)
{
	CleanupFinishedJobs();

	// Run the setup script (defines + instantiates _da_gen) on the game thread and
	// wait. A failed setup means the transformed script is broken and no job may
	// be created.
	TSharedPtr<FDaMCPExecResult> Setup = ExecuteOnGameThread(SetupCode);
	if (!Setup.IsValid() || !Setup->bOk)
	{
		OutError = Setup.IsValid() ? Setup->Error : TEXT("setup execution aborted (editor shutting down)");
		return -1;
	}

	TSharedPtr<FDaMCPJob> Job = MakeShareable(new FDaMCPJob());
	Job->JobId = NextJobId++;
	Job->StepCode = StepCode;
	Job->CancelCode = TEXT("try:\n    _da_gen.close()\nexcept Exception:\n    pass\nprint('DA_MCP_STATE|CANCELLED|' + str(_da_slices_done))");

	{
		FScopeLock Lock(&JobLock);
		Jobs.Add(Job->JobId, Job);
	}
	RegisterTicker();
	return Job->JobId;
}

bool FDAUnrealMCPBridge::PollJob(int32 JobId, FString& OutStatus, int32& OutSlicesDone, FString& OutError, FString& OutOutput)
{
	FScopeLock Lock(&JobLock);
	const TSharedPtr<FDaMCPJob>* Job = Jobs.Find(JobId);
	if (!Job || !Job->IsValid())
	{
		return false;
	}

	switch ((*Job)->State)
	{
	case EDaMCPJobState::Running:   OutStatus = TEXT("running");   break;
	case EDaMCPJobState::Done:      OutStatus = TEXT("done");      break;
	case EDaMCPJobState::Error:     OutStatus = TEXT("error");     break;
	case EDaMCPJobState::Cancelled: OutStatus = TEXT("cancelled"); break;
	}
	OutSlicesDone = (*Job)->SlicesDone;
	OutError = (*Job)->Error;
	OutOutput = (*Job)->Output;
	return true;
}

void FDAUnrealMCPBridge::RequestCancel(int32 JobId)
{
	FScopeLock Lock(&JobLock);
	const TSharedPtr<FDaMCPJob>* Job = Jobs.Find(JobId);
	if (Job && Job->IsValid())
	{
		(*Job)->bCancelRequested = true;
	}
}

bool FDAUnrealMCPBridge::OnGameThreadTick(float DeltaTime)
{
	TArray<TSharedPtr<FDaMCPJob>> Snapshot;
	{
		FScopeLock Lock(&JobLock);
		for (const auto& KV : Jobs)
		{
			Snapshot.Add(KV.Value);
		}
	}

	for (const TSharedPtr<FDaMCPJob>& Job : Snapshot)
	{
		if (!Job.IsValid() || Job->State != EDaMCPJobState::Running)
		{
			continue;
		}

		if (Job->bCancelRequested)
		{
			// Already on the game thread: close() raises GeneratorExit at the
			// suspended yield and runs finally blocks, so the transaction ends
			// cleanly and already-applied steps remain individually undoable.
			ExecuteInTransaction(Job->CancelCode);
			Job->State = EDaMCPJobState::Cancelled;
			continue;
		}

		const FDaMCPExecResult R = ExecuteInTransaction(Job->StepCode);
		ParseState(R.Log, *Job);
	}

	bool bAnyRunning = false;
	{
		FScopeLock Lock(&JobLock);
		for (const auto& KV : Jobs)
		{
			if (KV.Value->State == EDaMCPJobState::Running)
			{
				bAnyRunning = true;
				break;
			}
		}
	}
	if (!bAnyRunning)
	{
		UnregisterTicker();
	}
	return true;
}

void FDAUnrealMCPBridge::ParseState(const FString& Log, FDaMCPJob& Job)
{
	TArray<FString> Lines;
	Log.ParseIntoArrayLines(Lines);

	bool bCollectError = false;
	for (const FString& RawLine : Lines)
	{
		FString Line = RawLine.TrimStartAndEnd();
		if (Line.StartsWith(TEXT("DA_MCP_STATE|")))
		{
			bCollectError = false;
			const FString Rest = Line.RightChop(FCString::Strlen(TEXT("DA_MCP_STATE|")));
			TArray<FString> Parts;
			Rest.ParseIntoArray(Parts, TEXT("|"), true);
			const FString State = Parts.Num() > 0 ? Parts[0] : FString();
			if (Parts.Num() > 1)
			{
				Job.SlicesDone = FCString::Atoi(*Parts[1]);
			}

			if (State == TEXT("DONE"))
			{
				Job.State = EDaMCPJobState::Done;
			}
			else if (State == TEXT("ERROR"))
			{
				Job.State = EDaMCPJobState::Error;
				Job.Error.Reset();
				bCollectError = true;
			}
			else if (State == TEXT("CANCELLED"))
			{
				Job.State = EDaMCPJobState::Cancelled;
			}
		}
		else if (!Line.IsEmpty())
		{
			if (bCollectError)
			{
				if (!Job.Error.IsEmpty())
				{
					Job.Error += TEXT("\n");
				}
				Job.Error += Line;
			}
			else
			{
				if (!Job.Output.IsEmpty())
				{
					Job.Output += TEXT("\n");
				}
				Job.Output += Line;
			}
		}
	}
}

void FDAUnrealMCPBridge::RegisterTicker()
{
	if (TickerHandle.IsValid())
	{
		return;
	}
	// Use the (const FTickerDelegate&, float) overload — identical across
	// UE 5.4/5.5 (the (TCHAR*, float, TFunction) overload changed signature).
	TickerHandle = FTSTicker::GetCoreTicker().AddTicker(
		FTickerDelegate::CreateRaw(this, &FDAUnrealMCPBridge::OnGameThreadTick), 0.0f);
}

void FDAUnrealMCPBridge::UnregisterTicker()
{
	if (TickerHandle.IsValid())
	{
		FTSTicker::GetCoreTicker().RemoveTicker(TickerHandle);
		TickerHandle.Reset();
	}
}

void FDAUnrealMCPBridge::CleanupFinishedJobs()
{
	FScopeLock Lock(&JobLock);
	TArray<int32> ToRemove;
	for (const auto& KV : Jobs)
	{
		if (KV.Value->State != EDaMCPJobState::Running)
		{
			ToRemove.Add(KV.Key);
		}
	}
	for (int32 Key : ToRemove)
	{
		Jobs.Remove(Key);
	}
}
