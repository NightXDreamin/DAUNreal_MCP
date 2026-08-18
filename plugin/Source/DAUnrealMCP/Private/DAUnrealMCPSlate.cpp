// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUnrealMCPSlate.h"

#include "DAUnrealMCP.h"
#include "DAUnrealMCPBridge.h"

#include "Framework/Application/SlateApplication.h"
#include "Widgets/SWindow.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SBox.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SEditableTextBox.h"
#include "Widgets/Text/STextBlock.h"
#include "Widgets/SBoxPanel.h"
#include "Styling/CoreStyle.h"
#include "Modules/ModuleManager.h"

#define LOCTEXT_NAMESPACE "DAUnrealMCPSlate"

namespace
{
	TSharedPtr<SWindow> GPanelWindow;

	FDAUnrealMCPModule* GetModule()
	{
		if (FModuleManager::Get().IsModuleLoaded("DAUnrealMCP"))
		{
			return FModuleManager::GetModulePtr<FDAUnrealMCPModule>("DAUnrealMCP");
		}
		return nullptr;
	}

	/** Sanitize and parse port input from the text box. */
	int32 ParsePortInput(const TSharedPtr<SEditableTextBox>& Box)
	{
		if (!Box.IsValid())
		{
			return FDAUnrealMCPModule::GetConfiguredPort();
		}

		FString RawText = Box->GetText().ToString().TrimStartAndEnd();
		RawText.ReplaceInline(TEXT(","), TEXT(""));
		RawText.ReplaceInline(TEXT(" "), TEXT(""));
		RawText.ReplaceInline(TEXT("\t"), TEXT(""));

		const int32 Parsed = FCString::Atoi(*RawText);
		if (Parsed > 0 && Parsed <= 65535)
		{
			return Parsed;
		}
		return FDAUnrealMCPModule::GetConfiguredPort();
	}

	/** Build the panel content widget tree. */
	TSharedRef<SWidget> BuildPanelContent()
	{
		// Holders for dynamic widgets referenced by lambdas
		TSharedRef<TSharedPtr<SEditableTextBox>> PortEditHolder =
			MakeShared<TSharedPtr<SEditableTextBox>>();
		TSharedRef<TSharedPtr<SButton>> StartBtnHolder =
			MakeShared<TSharedPtr<SButton>>();
		TSharedRef<TSharedPtr<SButton>> StopBtnHolder =
			MakeShared<TSharedPtr<SButton>>();

		// Status text blocks
		TSharedRef<STextBlock> StatusText = SNew(STextBlock)
			.Text(LOCTEXT("StatusIdle", "● Not listening"))
			.Font(FCoreStyle::GetDefaultFontStyle("Bold", 10.5))
			.ColorAndOpacity(FSlateColor(FLinearColor(0.92f, 0.32f, 0.32f)));

		TSharedRef<STextBlock> AuthText = SNew(STextBlock)
			.Text(LOCTEXT("AuthIdle", "Token Auth: Inactive"))
			.Font(FCoreStyle::GetDefaultFontStyle("Regular", 9.0))
			.ColorAndOpacity(FSlateColor(FLinearColor(0.55f, 0.55f, 0.55f)));

		auto RefreshStatus = [StatusText, AuthText, StartBtnHolder, StopBtnHolder]() -> void
		{
			FDAUnrealMCPModule* Module = GetModule();
			const bool bIsRunning = Module && Module->GetBridge() && Module->GetBridge()->IsRunning();
			const int32 Port = (Module && Module->GetBridge()) ? Module->GetBridge()->GetPort() : FDAUnrealMCPModule::GetConfiguredPort();
			const bool bHasAuth = Module && Module->GetBridge() && Module->GetBridge()->HasAuthToken();

			if (bIsRunning)
			{
				StatusText->SetText(FText::Format(
					LOCTEXT("StatusOn", "● Listening on 127.0.0.1:{0}"),
					FText::FromString(FString::FromInt(Port))));
				StatusText->SetColorAndOpacity(FSlateColor(FLinearColor(0.25f, 0.85f, 0.45f)));

				AuthText->SetText(bHasAuth
					? LOCTEXT("AuthActive", "Token Auth: Active")
					: LOCTEXT("AuthDisabled", "Token Auth: Disabled"));
				AuthText->SetColorAndOpacity(FSlateColor(bHasAuth
					? FLinearColor(0.45f, 0.75f, 0.95f)
					: FLinearColor(0.6f, 0.6f, 0.6f)));
			}
			else
			{
				StatusText->SetText(LOCTEXT("StatusOff", "● Not listening"));
				StatusText->SetColorAndOpacity(FSlateColor(FLinearColor(0.92f, 0.32f, 0.32f)));

				AuthText->SetText(LOCTEXT("AuthInactive", "Token Auth: Inactive"));
				AuthText->SetColorAndOpacity(FSlateColor(FLinearColor(0.55f, 0.55f, 0.55f)));
			}

			if (StartBtnHolder->IsValid())
			{
				(*StartBtnHolder)->SetEnabled(!bIsRunning);
			}
			if (StopBtnHolder->IsValid())
			{
				(*StopBtnHolder)->SetEnabled(bIsRunning);
			}
		};

		TSharedPtr<SEditableTextBox>& PortEdit = *PortEditHolder;
		TSharedPtr<SButton>& StartBtn = *StartBtnHolder;
		TSharedPtr<SButton>& StopBtn = *StopBtnHolder;

		TSharedRef<SWidget> Content = SNew(SBorder)
			.BorderImage(FCoreStyle::Get().GetBrush("ToolPanel.GroupBorder"))
			.Padding(FMargin(14.0f, 12.0f, 14.0f, 14.0f))
			[
				SNew(SVerticalBox)

				// Header Row
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(0, 0, 0, 10.0f)
				[
					SNew(SHorizontalBox)

					+ SHorizontalBox::Slot()
					.FillWidth(1.0f)
					.VAlign(VAlign_Center)
					[
						SNew(SVerticalBox)

						+ SVerticalBox::Slot()
						.AutoHeight()
						[
							SNew(STextBlock)
							.Text(LOCTEXT("HeaderTitle", "DAUnreal MCP Bridge"))
							.Font(FCoreStyle::GetDefaultFontStyle("Bold", 13.0))
						]

						+ SVerticalBox::Slot()
						.AutoHeight()
						.Padding(0, 2.0f, 0, 0)
						[
							SNew(STextBlock)
							.Text(LOCTEXT("HeaderSubtitle", "Python Script Pass-Through"))
							.Font(FCoreStyle::GetDefaultFontStyle("Regular", 8.5))
							.ColorAndOpacity(FSlateColor(FLinearColor(0.55f, 0.55f, 0.55f)))
						]
					]

					+ SHorizontalBox::Slot()
					.AutoWidth()
					.VAlign(VAlign_Center)
					[
						SNew(SBorder)
						.BorderImage(FCoreStyle::Get().GetBrush("ToolPanel.DarkGroupBorder"))
						.Padding(FMargin(6.0f, 3.0f))
						[
							SNew(STextBlock)
							.Text(LOCTEXT("HeaderBadge", "NDJSON · TCP"))
							.Font(FCoreStyle::GetDefaultFontStyle("Bold", 8.0))
							.ColorAndOpacity(FSlateColor(FLinearColor(0.6f, 0.6f, 0.6f)))
						]
					]
				]

				// Status Card
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(0, 0, 0, 8.0f)
				[
					SNew(SBorder)
					.BorderImage(FCoreStyle::Get().GetBrush("ToolPanel.DarkGroupBorder"))
					.Padding(FMargin(12.0f, 8.0f))
					[
						SNew(SVerticalBox)

						+ SVerticalBox::Slot()
						.AutoHeight()
						.Padding(0, 0, 0, 3.0f)
						[
							StatusText
						]

						+ SVerticalBox::Slot()
						.AutoHeight()
						[
							AuthText
						]
					]
				]

				// Settings Card
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(0, 0, 0, 10.0f)
				[
					SNew(SBorder)
					.BorderImage(FCoreStyle::Get().GetBrush("ToolPanel.DarkGroupBorder"))
					.Padding(FMargin(12.0f, 6.0f))
					[
						SNew(SHorizontalBox)

						+ SHorizontalBox::Slot()
						.AutoWidth()
						.VAlign(VAlign_Center)
						.Padding(0, 0, 10.0f, 0)
						[
							SNew(STextBlock)
							.Text(LOCTEXT("PortLabel", "Port"))
							.Font(FCoreStyle::GetDefaultFontStyle("Regular", 9.5))
						]

						+ SHorizontalBox::Slot()
						.FillWidth(1.0f)
						.VAlign(VAlign_Center)
						[
							SAssignNew(PortEdit, SEditableTextBox)
							.Text(FText::FromString(FString::FromInt(FDAUnrealMCPModule::GetConfiguredPort())))
							.HintText(LOCTEXT("PortHint", "e.g. 8765"))
							.Font(FCoreStyle::GetDefaultFontStyle("Regular", 9.5))
							.OnTextCommitted_Lambda([PortEditHolder](const FText& InText, ETextCommit::Type InCommitType)
							{
								const int32 Validated = ParsePortInput(*PortEditHolder);
								if (PortEditHolder->IsValid())
								{
									(*PortEditHolder)->SetText(FText::FromString(FString::FromInt(Validated)));
								}
							})
						]
					]
				]

				// Action Buttons
				+ SVerticalBox::Slot()
				.AutoHeight()
				[
					SNew(SHorizontalBox)

					+ SHorizontalBox::Slot()
					.FillWidth(1.0f)
					.Padding(0, 0, 4.0f, 0)
					[
						SAssignNew(StartBtn, SButton)
						.HAlign(HAlign_Center)
						.VAlign(VAlign_Center)
						.ContentPadding(FMargin(0, 5.0f))
						.OnClicked_Lambda([PortEditHolder, RefreshStatus]() -> FReply
						{
							FDAUnrealMCPModule* Module = GetModule();
							const int32 Port = ParsePortInput(*PortEditHolder);
							if (PortEditHolder->IsValid())
							{
								(*PortEditHolder)->SetText(FText::FromString(FString::FromInt(Port)));
							}
							if (Module)
							{
								Module->RequestStart(Port);
							}
							RefreshStatus();
							return FReply::Handled();
						})
						[
							SNew(STextBlock)
							.Text(LOCTEXT("StartBtn", "Start"))
							.Font(FCoreStyle::GetDefaultFontStyle("Bold", 9.5))
						]
					]

					+ SHorizontalBox::Slot()
					.FillWidth(1.0f)
					.Padding(2.0f, 0, 2.0f, 0)
					[
						SAssignNew(StopBtn, SButton)
						.HAlign(HAlign_Center)
						.VAlign(VAlign_Center)
						.ContentPadding(FMargin(0, 5.0f))
						.OnClicked_Lambda([RefreshStatus]() -> FReply
						{
							FDAUnrealMCPModule* Module = GetModule();
							if (Module)
							{
								Module->RequestStop();
							}
							RefreshStatus();
							return FReply::Handled();
						})
						[
							SNew(STextBlock)
							.Text(LOCTEXT("StopBtn", "Stop"))
							.Font(FCoreStyle::GetDefaultFontStyle("Bold", 9.5))
						]
					]

					+ SHorizontalBox::Slot()
					.FillWidth(1.0f)
					.Padding(4.0f, 0, 0, 0)
					[
						SNew(SButton)
						.HAlign(HAlign_Center)
						.VAlign(VAlign_Center)
						.ContentPadding(FMargin(0, 5.0f))
						.OnClicked_Lambda([PortEditHolder, RefreshStatus]() -> FReply
						{
							FDAUnrealMCPModule* Module = GetModule();
							const int32 Port = ParsePortInput(*PortEditHolder);
							if (PortEditHolder->IsValid())
							{
								(*PortEditHolder)->SetText(FText::FromString(FString::FromInt(Port)));
							}
							if (Module)
							{
								Module->RequestRestart(Port);
							}
							RefreshStatus();
							return FReply::Handled();
						})
						[
							SNew(STextBlock)
							.Text(LOCTEXT("RestartBtn", "Restart"))
							.Font(FCoreStyle::GetDefaultFontStyle("Bold", 9.5))
						]
					]
				]
			];

		// Reflect initial state
		RefreshStatus();

		return Content;
	}
}

void FDAUnrealMCPSlate::ShowPanel()
{
	if (GPanelWindow.IsValid())
	{
		GPanelWindow->BringToFront();
		return;
	}

	TSharedRef<SWindow> Window = SNew(SWindow)
		.Title(LOCTEXT("WindowTitle", "DAUnreal MCP"))
		.ClientSize(FVector2D(360, 220))
		.SupportsMaximize(false)
		.SupportsMinimize(false)
		.AutoCenter(EAutoCenter::PreferredWorkArea);

	Window->SetContent(BuildPanelContent());

	FSlateApplication::Get().AddWindow(Window);
	GPanelWindow = Window;

	GPanelWindow->SetOnWindowClosed(FOnWindowClosed::CreateLambda([](const TSharedRef<SWindow>&)
	{
		GPanelWindow.Reset();
	}));
}

void FDAUnrealMCPSlate::ClosePanel()
{
	if (GPanelWindow.IsValid())
	{
		GPanelWindow->RequestDestroyWindow();
		GPanelWindow.Reset();
	}
}

#undef LOCTEXT_NAMESPACE
