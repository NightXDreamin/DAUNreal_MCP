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

	AsyncTask(ENamedThreads::GameThread, [Promise, Code]()
	{
		TSharedPtr<FDaMCPExecResult> R = MakeShareable(new FDaMCPExecResult());

		IPythonScriptPlugin* Py = IPythonScriptPlugin::Get();
		if (!Py || !Py->IsPythonAvailable())
		{
			R->bOk = false;
			R->Error = TEXT("PythonScriptPlugin is not available. Enable the 'Python Editor Script Plugin' and restart the editor.");
		}
		else
		{
			FPythonCommandEx Cmd;
			Cmd.Command = Code;
			Cmd.ExecutionMode = EPythonCommandExecutionMode::ExecuteFile;
			Cmd.FileExecutionScope = EPythonFileExecutionScope::Public;
			Cmd.Flags = EPythonCommandFlags::None;

			const bool bSuccess = Py->ExecPythonCommandEx(Cmd);

			R->bOk = bSuccess;
			R->Result = Cmd.CommandResult;

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
			R->Log = LogStr;
		}

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
