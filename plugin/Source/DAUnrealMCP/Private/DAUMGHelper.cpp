// Copyright Epic Games, Inc. All Rights Reserved.

#include "DAUMGHelper.h"

bool UDAUMGHelper::SetWidgetTreeRoot(UWidgetTree* Tree, UWidget* RootWidget)
{
	if (!Tree || !RootWidget)
	{
		return false;
	}

	Tree->Modify();
	Tree->RootWidget = RootWidget;
	RootWidget->Modify();

	return true;
}

bool UDAUMGHelper::SetWidgetIsVariable(UWidget* Widget, bool bIsVariable)
{
	if (!Widget)
	{
		return false;
	}

	Widget->Modify();
	Widget->bIsVariable = bIsVariable;
	return true;
}

TArray<UWidget*> UDAUMGHelper::GetAllWidgets(UWidgetTree* Tree)
{
	TArray<UWidget*> OutWidgets;
	if (Tree)
	{
		Tree->GetAllWidgets(OutWidgets);
	}
	return OutWidgets;
}
