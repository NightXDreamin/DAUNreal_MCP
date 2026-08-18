// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "Blueprint/WidgetTree.h"
#include "Components/Widget.h"
#include "DAUMGHelper.generated.h"

/**
 * Helper library for Editor Utility Widgets (EUW) and UMG manipulation from Python.
 * Unlocks protected/unexported properties in UWidgetTree and UWidget.
 */
UCLASS()
class DAUNREALMCP_API UDAUMGHelper : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/**
	 * Sets the root widget of a UWidgetTree.
	 * Solves the issue where UWidgetTree::RootWidget is protected and cannot be set via Python.
	 */
	UFUNCTION(BlueprintCallable, Category = "DAUnrealMCP|UMG")
	static bool SetWidgetTreeRoot(UWidgetTree* Tree, UWidget* RootWidget);

	/**
	 * Sets whether a UWidget is treated as a blueprint variable (bIsVariable).
	 * Solves the issue where UWidget::bIsVariable is not exposed to Python in UE5.
	 */
	UFUNCTION(BlueprintCallable, Category = "DAUnrealMCP|UMG")
	static bool SetWidgetIsVariable(UWidget* Widget, bool bIsVariable = true);

	/**
	 * Returns all widgets contained in a UWidgetTree.
	 */
	UFUNCTION(BlueprintCallable, Category = "DAUnrealMCP|UMG")
	static TArray<UWidget*> GetAllWidgets(UWidgetTree* Tree);
};
