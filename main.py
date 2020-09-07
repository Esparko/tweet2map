import tweepy
import sys
import argparse
from configparser import ConfigParser
import geopandas as gpd
import numpy as np
import pandas as pd
import pickle
import os

from src.SqlManagement import Tweet2MapDatabaseSQL
from src.LocationManagement import LocationDatabaseSQL
from src.CheckConfig import check_for_valid_config
from src.ArgparseProcessing import argparse_config
from src.ConnectTwitter import connect_to_twitter
from src.LoadTweets import load_tweets
from src.CheckDuplicateTweets import check_duplicate_tweets
from src.TweetParse import TweetParse
from src.AddNewLocation import add_new_location
from src.SpatialJoin import spatial_join
from src.CacheProcessing import cache_processing


if __name__ == '__main__':

    # Define work directory
    workspace = sys.argv[0]

    CONFIG_PATH = 'testconfig.ini'
    CACHE_PATH = 'tweet_cache.pkl'

    # Check for valid config file and load
    config = check_for_valid_config(CONFIG_PATH)

    # Define CLI inputs
    parser = argparse.ArgumentParser(description='Tweet2Map 1.0')
    cli_args = parser.add_argument_group('Arguments')
    # cli_args.add_argument('-v', help='Verbose mode', action='store_true')
    cli_args.add_argument('-d', help='Download tweets only and cache them for later processing', action='store_true')
    cli_args.add_argument('-consumer_key', help='Twitter API consumer key')
    cli_args.add_argument('-consumer_secret', help='Twitter API consumer secret')
    cli_args.add_argument('-access_token', help='Twitter API access token')
    cli_args.add_argument('-access_secret', help='Twitter API access secret')
    cli_args.add_argument('-inc_database_path', help='Incident database path')
    cli_args.add_argument('-shp_path', help='Shapefile path')
    cli_args.add_argument('-loc_database_path', help='Location database path')
    # Convert args to dict
    args = parser.parse_args()
    args = vars(args)

    # Process arguments
    tweepy_params = {}
    tweepy_params['consumer_key'] = argparse_config(arg=args['consumer_key'], section='tweepy', arg_type='consumer_key', config_path=CONFIG_PATH)
    tweepy_params['consumer_secret'] = argparse_config(arg=args['consumer_secret'], section='tweepy', arg_type='consumer_secret', config_path=CONFIG_PATH)
    tweepy_params['access_token'] = argparse_config(arg=args['access_token'], section='tweepy', arg_type='access_token', config_path=CONFIG_PATH)
    tweepy_params['access_secret'] = argparse_config(arg=args['access_secret'], section='tweepy', arg_type='access_secret', config_path=CONFIG_PATH)
    shp_path = argparse_config(arg=args['shp_path'], section='software', arg_type='shp_path', config_path=CONFIG_PATH)
    inc_database_path = argparse_config(arg=args['inc_database_path'], section='software', arg_type='database_path', config_path=CONFIG_PATH)
    loc_database_path = argparse_config(arg=args['loc_database_path'], section='software', arg_type='locations_path', config_path=CONFIG_PATH)
    download_only = args['d']

    # Connect to Tweepy
    api = connect_to_twitter(consumer_key=tweepy_params['consumer_key'],
                             consumer_secret=tweepy_params['consumer_secret'],
                             access_token=tweepy_params['access_token'],
                             access_secret=tweepy_params['access_secret'])

    # Load Tweets
    incoming_tweets = []
    tweets = load_tweets(api=api, screen_name='mmda', count=200)
    for tweet in reversed(tweets):
        if 'MMDA ALERT' in tweet.full_text:
            incoming_tweets.append(tweet)
    num_init_incoming_tweets = len(incoming_tweets)

    # Load SQL Database
    database_sql = Tweet2MapDatabaseSQL(sql_database_file=inc_database_path)
    recent_tweet_ids = database_sql.get_newest_tweet_ids(count=200)

    # Load cache for duplicate checking
    tweets_for_processing = []
    if os.path.exists(CACHE_PATH):
        
        # If cache exists load the file and combine with existing tweets
        with open(CACHE_PATH, 'rb') as f:
            tweet_cache = pickle.load(f)
        
        # Get IDs from cached and new tweets
        existing_cache_ids = [tweet.id_str for tweet in tweet_cache]
        incoming_tweet_ids = [tweet.id_str for tweet in incoming_tweets]
        
        tweets_for_processing += tweet_cache
    
        # Add incoming tweets but check if they exist first in cache
        for tweet in incoming_tweets:
            if tweet.id_str not in existing_cache_ids:
                tweets_for_processing.append(tweet)
    else:
        # No cache. So add all incoming tweets
        tweets_for_processing += incoming_tweets

    # Remove incoming tweets that are already in the incident database
    for idx, tweet in enumerate(tweets_for_processing):
        if tweet.id_str in recent_tweet_ids:
            del tweets_for_processing[idx]

    print(f'Downloaded {len(tweets_for_processing)} tweets')

    if download_only:
        # Download only and store to cache for later processing then exit
        cache_processing(cache_path=CACHE_PATH,
                         recent_processed_ids=recent_tweet_ids,
                         tweets=tweets_for_processing)
        sys.exit()
    

        
    # Load last n tweets to check for duplicates
    latest_tweet_ids = database_sql.get_newest_tweet_ids(count=200)

    # Load Locations
    location_sql = LocationDatabaseSQL(sql_database_file=loc_database_path)
    location_dict = location_sql.get_location_dictionary()

    # Process tweets
    process_counter = 0
    tweet_list = []  # Store processed tweets in list
    for tweet in tweets_for_processing:
        if 'MMDA ALERT' in tweet.full_text: # tweet.id_str not in existing_cache_ids:
            if tweet.id_str in recent_tweet_ids:
                print('Duplicate Data! Skipping to next tweet.')
                checkDuplicate = True
                continue
            else:
                tweet_text = tweet.full_text.upper()
                tweet_text = tweet_text.replace('  ', ' ')

                # Create TweetParse object then parse tweet
                twt = TweetParse(tweet)

                # Each individual tweet into a dict. Each unique dict will be appended to a list
                tweet_dict = {}
                tweet_dict['Tweet'] = twt.tweet_text
                tweet_dict['Date'] = twt.date
                tweet_dict['Time'] = twt.time
                tweet_dict['Source'] = twt.source
                tweet_dict['Location'] = twt.location
                tweet_dict['Direction'] = twt.direction
                tweet_dict['Type'] = twt.incident_type
                tweet_dict['Involved'] = twt.participants
                tweet_dict['Lanes_Blocked'] = twt.lanes_blocked
                tweet_list.append(tweet_dict)

    # Add unknown locations
    for idx, item in enumerate(tweet_list):

        # While loop will keep repeating until a valid choice is made with the unknown location
        # while loop handling
        bool_location_added = False
        bool_user_reset = False
        while not bool_location_added:
            try:
                if bool_user_reset:
                    # User reset due to revised name
                    location = location_revised
                    bool_user_reset = False
                    
                else:
                    location = item['Location']
                tweet_latitude = location_dict[location].split(',')[0]
                tweet_list[idx]['Latitude'] = tweet_latitude
                tweet_longitude = location_dict[location].split(',')[1]
                tweet_list[idx]['Longitude'] = tweet_longitude

                # Only count the valid data
                if (tweet_latitude and tweet_longitude) and (tweet_latitude != 'None' and tweet_longitude != 'None'):
                    process_counter += 1

                    print('---------------------------------------------------------------')
                    print('Tweet:', item['Tweet'])
                    print('Date:', item['Date'])
                    print('Time:', item['Time'])
                    print('URL:', item['Source'])
                    print('Location:', location)
                    print('Latitude:', tweet_latitude)
                    print('Longitude:', tweet_longitude)
                    print('Direction:', item['Direction'])
                    print('Incident Type:', item['Type'])
                    print('Participants:', item['Involved'])
                    print('Lanes Involved:', item['Lanes_Blocked'])
                else:
                    print(f'Skipping invalid location: {location}')
                
                break

            except KeyError:
                print('---------------------------------------------------------------')
                print(f'\nNew location detected! "{location}" is not recognized.')
                print(f'\nChoose an option from the list:')
                print('1 - Add new location and new coordinates')
                print(f'2 - Add new location based on existing coordinates')
                print(f'3 - Fix location name')
                print(f'4 - Set location as invalid\n')

                user_input_choice = str(input('Enter number to proceed:'))

                results = add_new_location(user_input_choice=user_input_choice,
                                        location=location,
                                        location_dict=location_dict,
                                        sql_object=location_sql)
                if results == 'BREAK':
                    continue
                if results[0] == 'REVISED':
                    bool_user_reset = True
                    location_revised = results[1]
                    continue
                    
                results_location = results[0]
                results_coords = results[1]
                location_dict = results[2]
                tweet_latitude = results[1].split(',')[0]
                tweet_longitude = results[1].split(',')[1]

                print(f'Data to be added:')
                print(f'Location: {location}')
                print(f'Latitude: {tweet_latitude}')
                print(f'Longitude: {tweet_longitude}')
                user_confirm_add = input('Confirm information is correct? (Y/N) ').upper()

                if user_confirm_add == 'Y':
                    location_dict[location] = f'{tweet_latitude},{tweet_longitude}'
                    location_sql.insert(location=location, coordinates=results_coords)
                    print('Added new location to location database')
                    break
                elif user_confirm_add == 'N':
                    break
                else:
                    print(f'Invalid input: {user_confirm_add}')
                    break

    # Spatial Join
    df = pd.DataFrame(tweet_list)
    df.replace(to_replace='None', value=np.nan, inplace=True)
    df.replace(to_replace='', value=np.nan, inplace=True)
    df.dropna(subset=['Latitude', 'Longitude'], inplace=True)
    df['Longitude'] = df['Longitude'].astype('float64')
    df['Latitude'] = df['Latitude'].astype('float64')
    df = spatial_join(df_input=df, shapefile=shp_path)

    print(f'\n{process_counter} new tweets added to database')

    # Update incident database
    for row in df.iterrows():
        database_sql.insert(row)

    # Close SQL connection
    database_sql.close_connection()

    # Delete cache if exists
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)