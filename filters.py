from aiogram import types
import requests
import json
from db import get_current_account, get_user_filters, set_user_filters, get_tokens, get_active_tokens
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import aiohttp
from meeff_http import create_meeff_session

# Global state for filter settings
user_filter_states = {}

async def get_meeff_filter_main_keyboard(user_id):
    """Main Meeff Filter menu with an efficient, horizontal account layout."""

    tokens = await get_tokens(user_id)
    
    # Get current filter status
    filter_enabled = user_filter_states.get(user_id, {}).get('request_filter_enabled', True)
    filter_status = " Enabled" if filter_enabled else " Disabled"
    
    keyboard = []
    
    # Filter toggle button at the top
    keyboard.append([
        InlineKeyboardButton(
            text=f" Request Filter: {filter_status}", 
            callback_data="toggle_request_filter"
        )
    ])
    
    ACCOUNTS_PER_ROW = 2
    row = []
    for i, token_data in enumerate(tokens):
        account_name = token_data.get('name', f'Account {i+1}')
        is_active = token_data.get('active', True)
        status_emoji = "✅" if is_active else "❌"
        
        filters = token_data.get('filters', {})
        nationality = filters.get('filterNationalityCode', '')
        nationality_display = f"({nationality})" if nationality else ""

        # Create the button and add it to the current row
        row.append(
            InlineKeyboardButton(
                text=f"{account_name} {nationality_display}",
                callback_data=f"account_filter_{i}"
            )
        )
        
        # When the row is full, add it to the keyboard
        if len(row) == ACCOUNTS_PER_ROW:
            keyboard.append(row)
            row = []

    # Add the last row if it's not full
    if row:
        keyboard.append(row)
    
    # Back button at the bottom
    keyboard.append([
        InlineKeyboardButton(text=" Back", callback_data="settings_menu")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_account_filter_keyboard(account_index):
    """Filter options for specific account"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Gender", callback_data=f"account_filter_gender_{account_index}"),
            InlineKeyboardButton(text="Age", callback_data=f"account_filter_age_{account_index}"),
            InlineKeyboardButton(text="Nationality", callback_data=f"account_filter_nationality_{account_index}")
        ],
        [
            InlineKeyboardButton(text=" Back to Accounts", callback_data="meeff_filter_main")
        ]
    ])
    return keyboard

def get_gender_keyboard(account_index):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="All Gender", callback_data=f"account_gender_all_{account_index}")],
        [InlineKeyboardButton(text="Male", callback_data=f"account_gender_male_{account_index}")],
        [InlineKeyboardButton(text="Female", callback_data=f"account_gender_female_{account_index}")],
        [InlineKeyboardButton(text=" Back", callback_data=f"account_filter_back_{account_index}")]
    ])
    return keyboard

def get_age_keyboard(account_index):
    keyboard = []
    # Create age buttons in rows of 5
    ages = list(range(18, 41))
    for i in range(0, len(ages), 5):
        row = []
        for age in ages[i:i+5]:
            row.append(InlineKeyboardButton(
                text=str(age), 
                callback_data=f"account_age_{age}_{account_index}"
            ))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text=" Back", callback_data=f"account_filter_back_{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_nationality_keyboard(account_index):
    countries = [
        ("RU", "🇷🇺"), ("UA", "🇺🇦"), ("BY", "🇧🇾"), ("IR", "🇮🇷"), ("PH", "🇵🇭"),
        ("PK", "🇵🇰"), ("US", "🇺🇸"), ("IN", "🇮🇳"), ("DE", "🇩🇪"), ("FR", "🇫🇷"),
        ("BR", "🇧🇷"), ("CN", "🇨🇳"), ("JP", "🇯🇵"), ("KR", "🇰🇷"), ("CA", "🇨🇦"),
        ("AU", "🇦🇺"), ("IT", "🇮🇹"), ("ES", "🇪🇸"), ("ZA", "🇿🇦"), ("TR", "🇹🇷")
    ]
    keyboard = []
    # "All Countries" button on its own row
    keyboard.append([InlineKeyboardButton(text="All Countries", callback_data=f"account_nationality_all_{account_index}")])
    
    NATIONALITIES_PER_ROW = 5
    row = []
    for country, flag in countries:
        row.append(InlineKeyboardButton(
            text=f"{flag} {country}", 
            callback_data=f"account_nationality_{country}_{account_index}"
        ))
        # When the row is full, add it to the keyboard and start a new one
        if len(row) == NATIONALITIES_PER_ROW:
            keyboard.append(row)
            row = []

    # Add any remaining buttons in the last row if it's not empty
    if row:
        keyboard.append(row)
    
    # Back button on its own row
    keyboard.append([InlineKeyboardButton(text=" Back", callback_data=f"account_filter_back_{account_index}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def apply_filter_for_account(token, user_id):
    """Apply stored filters for a specific account using simplified headers."""
    try:
        user_filters = await get_user_filters(user_id, token) or {}
        
        # Default filter data
        filter_data = {
            "filterGenderType": user_filters.get("filterGenderType", 5),
            "filterBirthYearFrom": user_filters.get("filterBirthYearFrom", 1979),
            "filterBirthYearTo": 2006,
            "filterDistance": 510,
            "filterLanguageCodes": user_filters.get("filterLanguageCodes", ""),
            "filterNationalityBlock": user_filters.get("filterNationalityBlock", 0),
            "filterNationalityCode": user_filters.get("filterNationalityCode", ""),
            "locale": "en"
        }
        
        url = "https://api.meeff.com/user/updateFilter/v1"
        
        # --- SIMPLIFIED HEADERS  ---
        headers = {
            'User-Agent': "okhttp/5.1.0", # Updated User-Agent
            'Accept-Encoding': "gzip",
            'meeff-access-token': token,
            'content-type': "application/json; charset=utf-8"
        }
        # --------------------------------------------------------
        
        async with create_meeff_session() as session:
            async with session.post(url, json=filter_data, headers=headers) as response:
                if response.status == 200:
                    print(f" Filter applied successfully for token: {token[:10]}...")
                    return True
                else:
                    print(f" Failed to apply filter for token: {token[:10]}... Status: {response.status}")
                    return False
                    
    except Exception as e:
        print(f" Error applying filter: {e}")
        return False

async def set_account_filter(callback_query: types.CallbackQuery):
    """Handle account-specific filter settings"""
    user_id = callback_query.from_user.id
    
    tokens = await get_tokens(user_id)
    
    # Parse callback data
    data_parts = callback_query.data.split('_')
    
    if callback_query.data == "toggle_request_filter":
        # Toggle the request filter enable/disable
        if user_id not in user_filter_states:
            user_filter_states[user_id] = {}
        
        current_status = user_filter_states[user_id].get('request_filter_enabled', True)
        user_filter_states[user_id]['request_filter_enabled'] = not current_status
        
        await callback_query.message.edit_text(
            "🎛️ <b>Meeff Filter Settings</b>\n\n"
            "Configure filters for each account and enable/disable request filtering:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML"
        )
        await callback_query.answer(f"Request filter {'enabled' if not current_status else 'disabled'}!")
        return True
    
    elif callback_query.data == "meeff_filter_main":
        await callback_query.message.edit_text(
            "🎛️ <b>Meeff Filter Settings</b>\n\n"
            "Configure filters for each account and enable/disable request filtering:",
            reply_markup=await get_meeff_filter_main_keyboard(user_id),
            parse_mode="HTML"
        )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_filter_") and not any(x in callback_query.data for x in ['gender', 'age', 'nationality', 'back']):
        # Show filter options for specific account
        account_index = int(data_parts[2])
        if account_index < len(tokens):
            account_name = tokens[account_index].get('name', f'Account {account_index + 1}')
            await callback_query.message.edit_text(
                f"🎛️ <b>Filter Settings for {account_name}</b>\n\n"
                "Choose what to configure:",
                reply_markup=get_account_filter_keyboard(account_index),
                parse_mode="HTML"
            )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_filter_gender_"):
        account_index = int(data_parts[3])
        await callback_query.message.edit_text(
            " <b>Select Gender Filter:</b>",
            reply_markup=get_gender_keyboard(account_index),
            parse_mode="HTML"
        )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_filter_age_"):
        account_index = int(data_parts[3])
        await callback_query.message.edit_text(
            " <b>Select Age Filter:</b>",
            reply_markup=get_age_keyboard(account_index),
            parse_mode="HTML"
        )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_filter_nationality_"):
        account_index = int(data_parts[3])
        await callback_query.message.edit_text(
            " <b>Select Nationality Filter:</b>",
            reply_markup=get_nationality_keyboard(account_index),
            parse_mode="HTML"
        )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_filter_back_"):
        account_index = int(data_parts[3])
        if account_index < len(tokens):
            account_name = tokens[account_index].get('name', f'Account {account_index + 1}')
            await callback_query.message.edit_text(
                f"🎛️ <b>Filter Settings for {account_name}</b>\n\n"
                "Choose what to configure:",
                reply_markup=get_account_filter_keyboard(account_index),
                parse_mode="HTML"
            )
        await callback_query.answer()
        return True
    
    # Handle specific filter selections
    elif callback_query.data.startswith("account_gender_"):
        parts = callback_query.data.split('_')
        gender = parts[2]
        account_index = int(parts[3])
        
        if account_index < len(tokens):
            token = tokens[account_index]['token']
            account_name = tokens[account_index].get('name', f'Account {account_index + 1}')
            
            user_filters = await get_user_filters(user_id, token) or {}
            
            # Update gender filter
            if gender == "male":
                user_filters["filterGenderType"] = 6
            elif gender == "female":
                user_filters["filterGenderType"] = 5
            elif gender == "all":
                user_filters["filterGenderType"] = 7
            
            # Save filters
            await set_user_filters(user_id, token, user_filters)
            
            # Apply filter immediately
            await apply_filter_for_account(token, user_id)
            
            await callback_query.message.edit_text(
                f" <b>Gender filter updated for {account_name}</b>\n\n"
                f"Gender set to: {gender.capitalize()}",
                reply_markup=get_account_filter_keyboard(account_index),
                parse_mode="HTML"
            )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_age_"):
        parts = callback_query.data.split('_')
        age = int(parts[2])
        account_index = int(parts[3])
        
        if account_index < len(tokens):
            token = tokens[account_index]['token']
            account_name = tokens[account_index].get('name', f'Account {account_index + 1}')
            
            user_filters = await get_user_filters(user_id, token) or {}
            
            # Update age filter
            current_year = 2025 
            user_filters["filterBirthYearFrom"] = current_year - age
            
            # Save filters
            await set_user_filters(user_id, token, user_filters)
            
            # Apply filter immediately
            await apply_filter_for_account(token, user_id)
            
            await callback_query.message.edit_text(
                f"<b>Age filter updated for {account_name}</b>\n\n"
                f"Age set to: {age}",
                reply_markup=get_account_filter_keyboard(account_index),
                parse_mode="HTML"
            )
        await callback_query.answer()
        return True
    
    elif callback_query.data.startswith("account_nationality_"):
        parts = callback_query.data.split('_')
        nationality = parts[2]
        account_index = int(parts[3])
        
        if account_index < len(tokens):
            token = tokens[account_index]['token']
            account_name = tokens[account_index].get('name', f'Account {account_index + 1}')
            
            user_filters = await get_user_filters(user_id, token) or {}
            
            # Update nationality filter
            if nationality == "all":
                user_filters["filterNationalityCode"] = ""
            else:
                user_filters["filterNationalityCode"] = nationality
            
            # Save filters
            await set_user_filters(user_id, token, user_filters)
            
            # Apply filter immediately
            await apply_filter_for_account(token, user_id)
            
            nationality_display = nationality.upper() if nationality != "all" else "All Countries"
            await callback_query.message.edit_text(
                f" <b>Nationality filter updated for {account_name}</b>\n\n"
                f"Nationality set to: {nationality_display}",
                reply_markup=get_account_filter_keyboard(account_index),
                parse_mode="HTML"
            )
        await callback_query.answer()
        return True
    
    return False

async def meeff_filter_command(message: types.Message):
    """Main command to show Meeff Filter settings"""
    user_id = message.from_user.id
    await message.answer(
        "🎛️ <b>Meeff Filter Settings</b>\n\n"
        "Configure filters for each account and enable/disable request filtering:",
        reply_markup=await get_meeff_filter_main_keyboard(user_id),
        parse_mode="HTML"
    )

def is_request_filter_enabled(user_id):
    """Check if request filter is enabled for user"""
    return user_filter_states.get(user_id, {}).get('request_filter_enabled', True)

# Legacy functions for backward compatibility
async def set_filter(callback_query: types.CallbackQuery):
    """Legacy function - redirect to new system"""
    return await set_account_filter(callback_query)

async def filter_command(message: types.Message):
    """Legacy function - redirect to new system"""
    return await meeff_filter_command(message)
