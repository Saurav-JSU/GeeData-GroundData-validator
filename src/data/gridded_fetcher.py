#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
import time
from typing import Dict, Any, Optional, Callable, List, Union
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import json

from src.base_fetcher import DataFetcher
from config import GriddedDataConfig, GriddedDatasetConfig

# Conditional import for Earth Engine
try:
    import ee
    EARTH_ENGINE_AVAILABLE = True
except ImportError:
    EARTH_ENGINE_AVAILABLE = False

logger = logging.getLogger(__name__)

class GriddedDataFetcher(DataFetcher):
    """
    Fetches gridded data from Earth Engine with progress reporting capabilities.
    
    Handles multiple gridded datasets (ERA5, DAYMET, PRISM, CHIRPS, FLDAS, GSMAP, GLDAS)
    with different temporal resolutions and conversion factors.
    """
    
    def __init__(self, config: GriddedDataConfig):
        self.config = config
        self.progress_callback = None
        self._ee_initialized = False
        
    def set_progress_callback(self, callback: Callable[[str, int], None]):
        """
        Set a callback function for progress reporting
        
        Args:
            callback: Function that takes dataset_name and progress percentage
        """
        self.progress_callback = callback
        
    def _initialize_earth_engine(self) -> bool:
        """Initialize Earth Engine API with project ID"""
        if not EARTH_ENGINE_AVAILABLE:
            logger.warning("Earth Engine API not available. Install with: pip install earthengine-api")
            return False
            
        if self._ee_initialized:
            return True
            
        try:
            # Initialize Earth Engine with the project ID from config
            project_id = self.config.ee_project_id
            logger.info(f"Initializing Earth Engine with project ID: {project_id}")
            
            ee.Initialize(project=project_id)
            self._ee_initialized = True
            logger.info("Earth Engine initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Earth Engine: {str(e)}")
            return False
        
    def fetch_data(self) -> Dict[str, pd.DataFrame]:
        """Fetch gridded data for enabled datasets with progress reporting"""
        results = {}
        enabled_datasets = self.config.get_enabled_datasets()
        
        # Initialize Earth Engine if needed
        ee_available = self._initialize_earth_engine()
        if not ee_available:
            logger.error("Earth Engine initialization failed. Cannot fetch real data.")
            return results
        
        for dataset in enabled_datasets:
            try:
                logger.info(f"Fetching {dataset.name} data...")
                
                if self.progress_callback:
                    self.progress_callback(dataset.name, 0)
                
                # Check if data already exists
                output_path = Path(self.config.data_dir) / dataset.get_filename()
                if output_path.exists():
                    logger.info(f"Loading existing {dataset.name} data from {output_path}")
                    
                    # Load existing data
                    df = pd.read_csv(output_path, index_col=0)
                    df.index = pd.to_datetime(df.index)
                    results[dataset.name] = df
                    
                    if self.progress_callback:
                        self.progress_callback(dataset.name, 100)
                    
                    continue
                
                # Check for date range constraints for specific datasets
                if self._should_skip_dataset(dataset):
                    logger.warning(f"Skipping {dataset.name} - outside valid date range")
                    continue
                
                # Use appropriate fetching method based on dataset characteristics
                if dataset.time_scale == "monthly":
                    data = self._fetch_monthly_dataset(dataset)
                elif dataset.time_scale in ["hourly", "3hourly"] or dataset.name in ["GSMAP", "GLDAS-Historical", "GLDAS-Current"]:
                    # For high-resolution datasets or specific datasets prone to memory issues
                    data = self._fetch_high_resolution_dataset(dataset)
                else:
                    # For standard daily datasets
                    data = self._fetch_ee_dataset(dataset)
                
                results[dataset.name] = data
                
                if self.progress_callback:
                    self.progress_callback(dataset.name, 100)
                    
            except Exception as e:
                logger.error(f"Error fetching {dataset.name} data: {e}", exc_info=True)
                if self.progress_callback:
                    self.progress_callback(dataset.name, 0)
                continue
                
        return results
    
    def _should_skip_dataset(self, dataset: GriddedDatasetConfig) -> bool:
        """Check if dataset should be skipped based on date range constraints"""
        if dataset.date_range is None:
            return False
            
        start_year, end_year = dataset.date_range
        if end_year is None:  # Open-ended range
            return self.config.end_year < start_year
        else:
            return (self.config.start_year > end_year or 
                    self.config.end_year < start_year)
    
    def _load_station_metadata(self) -> pd.DataFrame:
        """Load station metadata from file"""
        metadata_file = Path(self.config.data_dir) / "stations_metadata.csv"
        if not metadata_file.exists():
            raise FileNotFoundError(f"Station metadata file not found: {metadata_file}")
            
        return pd.read_csv(metadata_file)
    
    def _aggregate_to_daily(self, image_collection, dataset: GriddedDatasetConfig) -> ee.ImageCollection:
        """Aggregate sub-daily data to daily in Earth Engine with dataset-specific handling"""
        logger.info(f"Aggregating {dataset.time_scale} data to daily in Earth Engine for {dataset.name}...")
        
        # Define a function to extract date string and set it as a property
        def add_date_property(image):
            date = ee.Date(image.get('system:time_start'))
            day_string = date.format('YYYY-MM-dd')
            return image.set('date', day_string)
        
        # Add date property to each image
        image_collection = image_collection.map(add_date_property)
        
        # Get list of unique dates
        dates = image_collection.aggregate_array('date').distinct()
        
        # Define dataset-specific aggregation functions
        def sum_day(day):
            day_collection = image_collection.filter(ee.Filter.eq('date', day))
            
            # Handle dataset-specific aggregation logic
            if dataset.name == 'GSMAP':
                # For GSMAP, directly sum the hourly precipitation rates (mm/hr) to get daily total (mm/day)
                # Each hour contributes 1 hour's worth of rain at the given rate
                day_sum = day_collection.map(lambda img: img.multiply(1.0)).sum()
            elif dataset.name.startswith('GLDAS'):
                # For GLDAS (both Historical and Current), special handling needed
                if dataset.time_scale == "3hourly":
                    # For 3-hourly data, each image covers 3 hours (1/8 of day)
                    # Scale each image by 3hours/24hours = 0.125 before summing
                    day_sum = day_collection.map(lambda img: img.multiply(0.125 * dataset.conversion_factor)).sum()
                else:
                    # For other GLDAS temporal resolutions
                    day_sum = day_collection.sum().multiply(dataset.conversion_factor)
            else:
                # For other datasets with hourly data (standard handling)
                if dataset.time_scale == "hourly":
                    # For hourly data, each image is 1/24 of a day
                    day_sum = day_collection.map(lambda img: img.multiply(1.0/24.0)).sum()
                    # Apply conversion factor after summing if needed
                    if dataset.conversion_factor != 1.0:
                        day_sum = day_sum.multiply(dataset.conversion_factor)
                else:
                    # For other temporal resolutions, just sum then apply conversion
                    day_sum = day_collection.sum()
                    if dataset.conversion_factor != 1.0:
                        day_sum = day_sum.multiply(dataset.conversion_factor)
            
            # Keep the date as a property and set time to midnight
            day_date = ee.Date.parse('YYYY-MM-dd', day)
            return day_sum.set({
                'system:time_start': day_date.millis(),
                'date': day
            })
        
        # Create new daily collection by processing the sub-daily data
        daily_collection = ee.ImageCollection(dates.map(sum_day))
        
        logger.info(f"Successfully aggregated {dataset.name} to daily data in Earth Engine")
        return daily_collection
    
    def _fetch_monthly_dataset(self, dataset: GriddedDatasetConfig) -> pd.DataFrame:
        """Fetch monthly data from Earth Engine (special case for FLDAS)"""
        if not EARTH_ENGINE_AVAILABLE:
            raise ImportError("Earth Engine API not available")
        
        # Report start of processing
        if self.progress_callback:
            self.progress_callback(dataset.name, 5)
            
        # Create date range for the query
        start_date = f"{self.config.start_year}-01-01"
        end_date = f"{self.config.end_year}-12-31"
        
        logger.info(f"Fetching monthly {dataset.name} data from {start_date} to {end_date}")
        
        # Get the ImageCollection
        collection_name = dataset.collection_name
        variable_name = dataset.variable_name
        
        # Create the image collection
        image_collection = ee.ImageCollection(collection_name) \
            .select(variable_name) \
            .filterDate(start_date, end_date)
        
        # Update progress
        if self.progress_callback:
            self.progress_callback(dataset.name, 15)
        
        # Load metadata for ground stations
        metadata = self._load_station_metadata()
        stations = metadata[['id', 'latitude', 'longitude']].dropna()
        
        logger.info(f"Extracting monthly data for {len(stations)} stations")
        
        # Create full monthly date range for the dataframe
        full_date_range = pd.date_range(
            start=pd.to_datetime(start_date).replace(day=1),
            end=pd.to_datetime(end_date).replace(day=28),
            freq='MS'  # Month start frequency
        )
        
        # Create empty DataFrame with stations as columns and dates as index
        result_df = pd.DataFrame(index=full_date_range, columns=stations['id'].tolist())
        
        # Get list of all months in the collection
        date_list = []
        dates = image_collection.aggregate_array('system:time_start').getInfo()
        
        # Convert milliseconds since epoch to date strings (first of month)
        for d in dates:
            dt = datetime.utcfromtimestamp(d/1000)
            date_strings = dt.replace(day=1).strftime('%Y-%m-%d')
            if date_strings not in date_list:
                date_list.append(date_strings)
        
        # Sort dates
        date_list.sort()
        
        # Process each month
        for i, date_str in enumerate(date_list):
            # Update progress
            progress = 15 + ((i / len(date_list)) * 80)
            if self.progress_callback:
                self.progress_callback(dataset.name, int(progress))
            
            # Get the first day of next month
            month_date = datetime.strptime(date_str, '%Y-%m-%d')
            if month_date.month == 12:
                next_month = month_date.replace(year=month_date.year+1, month=1)
            else:
                next_month = month_date.replace(month=month_date.month+1)
            next_month_str = next_month.strftime('%Y-%m-%d')
            
            # Filter collection to this month
            monthly_image = image_collection.filterDate(date_str, next_month_str).first()
            
            if monthly_image is None:
                logger.warning(f"No data available for {date_str}")
                continue
                
            # Create list of points to sample
            points = [ee.Geometry.Point(row['longitude'], row['latitude']) for _, row in stations.iterrows()]
            
            # Sample the image at all station points
            try:
                point_values = monthly_image.sampleRegions(
                    collection=ee.FeatureCollection(points),
                    properties=['system:index'],
                    scale=1000  # Scale in meters
                ).getInfo()
                
                # Extract values for each station
                date = pd.to_datetime(date_str)
                days_in_month = pd.Period(date, freq='M').days_in_month
                
                for i, feature in enumerate(point_values.get('features', [])):
                    if variable_name in feature['properties']:
                        station_id = stations.iloc[i]['id']
                        value = feature['properties'][variable_name]
                        
                        # Convert kg/m²/s → mm/month:
                        # First apply config conversion factor (86400) to get mm/day
                        # Then multiply by days in month to get mm/month
                        converted_value = value * dataset.conversion_factor * days_in_month
                        
                        if date in result_df.index:
                            result_df.loc[date, station_id] = converted_value
                
            except Exception as e:
                logger.error(f"Error sampling points for {date_str}: {str(e)}")
        
        # Final progress update
        if self.progress_callback:
            self.progress_callback(dataset.name, 95)
            
        return result_df
    
    def _fetch_high_resolution_dataset(self, dataset: GriddedDatasetConfig) -> pd.DataFrame:
        """
        Fetch data for high temporal resolution datasets (hourly, 3-hourly)
        using a month-by-month approach to avoid memory limits
        """
        if not EARTH_ENGINE_AVAILABLE:
            raise ImportError("Earth Engine API not available")
        
        # Report start of processing
        if self.progress_callback:
            self.progress_callback(dataset.name, 5)
            
        # Create date range parameters
        start_year = self.config.start_year
        end_year = self.config.end_year
        
        logger.info(f"Fetching {dataset.name} data from {start_year} to {end_year} using chunked approach")
        
        # Load metadata for ground stations
        metadata = self._load_station_metadata()
        stations = metadata[['id', 'latitude', 'longitude']].dropna()
        
        logger.info(f"Extracting data for {len(stations)} stations")
        
        # Create full date range for the dataframe (daily resolution for result)
        start_date = f"{start_year}-01-01"
        end_date = f"{end_year}-12-31"
        full_date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        
        # Create empty DataFrame with stations as columns and dates as index
        result_df = pd.DataFrame(index=full_date_range, columns=stations['id'].tolist())
        
        # Get collection info
        collection_name = dataset.collection_name
        variable_name = dataset.variable_name
        
        # Process data month by month to avoid memory limits
        total_months = (end_year - start_year + 1) * 12
        month_count = 0
        
        # Iterate through each month in the date range
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                # Update progress based on months processed
                month_count += 1
                progress = 10 + ((month_count / total_months) * 85)
                if self.progress_callback:
                    self.progress_callback(dataset.name, int(progress))
                
                # Define month start and end dates
                month_start = f"{year}-{month:02d}-01"
                # Calculate next month (handling December)
                if month == 12:
                    next_month_start = f"{year+1}-01-01"
                else:
                    next_month_start = f"{year}-{month+1:02d}-01"
                
                try:
                    logger.info(f"Processing {dataset.name} for {month_start}")
                    
                    # Get data for this month
                    image_collection = ee.ImageCollection(collection_name) \
                        .select(variable_name) \
                        .filterDate(month_start, next_month_start)
                    
                    # For sub-daily data, aggregate to daily in GEE
                    image_collection = self._aggregate_to_daily(image_collection, dataset)
                    
                    # Get dates in this month (this should be a smaller, manageable list)
                    try:
                        month_dates = image_collection.aggregate_array('system:time_start').getInfo()
                        date_strings = [datetime.utcfromtimestamp(d/1000).strftime('%Y-%m-%d') 
                                    for d in month_dates]
                        # Sort dates
                        date_strings.sort()
                    except Exception as e:
                        logger.warning(f"Error getting dates for {month_start}: {str(e)}")
                        continue
                    
                    # Process each day in this month
                    for date_str in date_strings:
                        # Get daily image
                        next_day_str = self._next_day(date_str)
                        daily_image = image_collection.filterDate(date_str, next_day_str).first()
                        
                        if daily_image is None:
                            continue
                        
                        # Create list of points to sample
                        points = [ee.Geometry.Point(row['longitude'], row['latitude']) 
                                for _, row in stations.iterrows()]
                        
                        # Sample the image at all station points
                        try:
                            point_values = daily_image.sampleRegions(
                                collection=ee.FeatureCollection(points),
                                properties=['system:index'],
                                scale=1000  # Scale in meters
                            ).getInfo()
                            
                            # Extract values for each station
                            date = pd.to_datetime(date_str)
                            for i, feature in enumerate(point_values.get('features', [])):
                                if variable_name in feature['properties']:
                                    station_id = stations.iloc[i]['id']
                                    value = feature['properties'][variable_name]
                                    
                                    # No need for additional conversion factor here
                                    # The conversion has already been applied during aggregation
                                    
                                    if date in result_df.index:
                                        result_df.loc[date, station_id] = value
                        
                        except Exception as e:
                            logger.warning(f"Error sampling points for {date_str}: {str(e)}")
                
                except Exception as e:
                    logger.warning(f"Error processing {dataset.name} for {month_start}: {str(e)}")
                    continue
        
        # Final progress update
        if self.progress_callback:
            self.progress_callback(dataset.name, 95)
            
        return result_df
        
    def _fetch_ee_dataset(self, dataset: GriddedDatasetConfig) -> pd.DataFrame:
        """Fetch data from Earth Engine for a specific dataset with appropriate aggregation"""
        if not EARTH_ENGINE_AVAILABLE:
            raise ImportError("Earth Engine API not available")
        
        # Report start of processing
        if self.progress_callback:
            self.progress_callback(dataset.name, 5)
            
        # Create date range for the query
        start_date = f"{self.config.start_year}-01-01"
        end_date = f"{self.config.end_year}-12-31"
        
        logger.info(f"Fetching {dataset.name} data from {start_date} to {end_date}")
        
        # Define the collection and get the ImageCollection
        collection_name = dataset.collection_name
        variable_name = dataset.variable_name
        
        # Create the image collection
        image_collection = ee.ImageCollection(collection_name) \
            .select(variable_name) \
            .filterDate(start_date, end_date)
        
        # Update progress
        if self.progress_callback:
            self.progress_callback(dataset.name, 15)
        
        # Load metadata for ground stations
        metadata = self._load_station_metadata()
        stations = metadata[['id', 'latitude', 'longitude']].dropna()
        
        logger.info(f"Extracting data for {len(stations)} stations")
        
        # Get list of all dates in the collection
        date_list = self._get_date_list(image_collection)
        
        # Create full date range for the dataframe
        full_date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        
        # Create empty DataFrame with stations as columns and dates as index
        result_df = pd.DataFrame(index=full_date_range, columns=stations['id'].tolist())
        
        # Process data in batches to avoid timeout issues
        batch_size = 100  # Process 100 days at a time
        total_batches = len(date_list) // batch_size + (1 if len(date_list) % batch_size > 0 else 0)
        
        # Process each batch
        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch_end = min((batch_idx + 1) * batch_size, len(date_list))
            batch_dates = date_list[batch_start:batch_end]
            
            # Update progress based on batch
            batch_progress = 15 + ((batch_idx / total_batches) * 80)
            if self.progress_callback:
                self.progress_callback(dataset.name, int(batch_progress))
            
            # Process this batch of dates
            batch_data = self._process_date_batch(
                image_collection, batch_dates, stations, variable_name, dataset
            )
            
            # Add batch data to result dataframe
            for date_str, station_values in batch_data.items():
                date = pd.to_datetime(date_str)
                if date in result_df.index:
                    for station_id, value in station_values.items():
                        result_df.loc[date, station_id] = value
            
            logger.info(f"Processed batch {batch_idx+1}/{total_batches} for {dataset.name}")
        
        # Apply conversion factor if needed
        if dataset.conversion_factor != 1.0:
            result_df = result_df.mul(dataset.conversion_factor)
        
        # Final progress update
        if self.progress_callback:
            self.progress_callback(dataset.name, 95)
            
        return result_df
    
    def _get_date_list(self, image_collection) -> List[str]:
        """Get list of dates in the image collection"""
        # Get distinct dates from the collection
        dates = image_collection.aggregate_array('system:time_start').getInfo()
        
        # Convert milliseconds since epoch to date strings
        date_strings = [datetime.utcfromtimestamp(d/1000).strftime('%Y-%m-%d') for d in dates]
        
        # Sort dates
        date_strings.sort()
        
        return date_strings
        
    def _process_date_batch(self, image_collection, dates, stations, variable_name, dataset) -> Dict[str, Dict[str, float]]:
        """Process a batch of dates to extract point values for all stations"""
        result = {}
        
        for date_str in dates:
            # Filter collection to this date
            next_day_str = self._next_day(date_str)
            daily_image = image_collection.filterDate(date_str, next_day_str).first()
            
            if daily_image is None:
                logger.warning(f"No data available for {date_str} in {dataset.name}")
                continue
                
            # Create list of points to sample
            points = [ee.Geometry.Point(row['longitude'], row['latitude']) for _, row in stations.iterrows()]
            
            # Sample the image at all station points
            try:
                point_values = daily_image.sampleRegions(
                    collection=ee.FeatureCollection(points),
                    properties=['system:index'],
                    scale=1000  # Scale in meters
                ).getInfo()
                
                # Extract values for each station
                station_values = {}
                for i, feature in enumerate(point_values.get('features', [])):
                    if variable_name in feature['properties']:
                        station_id = stations.iloc[i]['id']
                        value = feature['properties'][variable_name]
                        station_values[station_id] = value
                
                result[date_str] = station_values
                
            except Exception as e:
                logger.error(f"Error sampling points for {date_str}: {str(e)}")
                
        return result
        
    def _next_day(self, date_str) -> str:
        """Get the next day after the given date string"""
        date = datetime.strptime(date_str, '%Y-%m-%d')
        next_day = date + timedelta(days=1)
        return next_day.strftime('%Y-%m-%d')
        
    def validate_data(self, data: Dict[str, pd.DataFrame]) -> bool:
        """Validate the fetched data"""
        if not data:
            return False
            
        for name, df in data.items():
            if df.empty:
                logger.error(f"Empty dataset: {name}")
                return False
            if not isinstance(df.index, pd.DatetimeIndex):
                logger.error(f"Invalid index type for dataset: {name}")
                return False
                
        return True
        
    def save_data(self, data: Dict[str, pd.DataFrame], path: Optional[str] = None) -> None:
        """Save the data to CSV files"""
        for name, df in data.items():
            # Get filename from dataset config
            dataset_config = next((ds for ds in self.config.datasets.values() if ds.name == name), None)
            if not dataset_config:
                logger.warning(f"No configuration found for dataset {name}")
                continue
                
            filename = dataset_config.get_filename()
            file_path = Path(self.config.data_dir) / filename
            
            # Save data
            df.to_csv(file_path)
            logger.info(f"Saved {name} data to {file_path}")
            
    def process(self) -> Dict[str, pd.DataFrame]:
        """Main processing method with progress reporting"""
        logger.info("Fetching gridded data...")
        
        # Ensure there are enabled datasets
        if not self.config.is_valid():
            raise ValueError("No gridded datasets enabled")
            
        # Fetch data
        data = self.fetch_data()
        
        # Validate data
        if not self.validate_data(data):
            raise ValueError("Gridded data validation failed")
            
        # Save data
        self.save_data(data)
        
        return data
        
    def get_summary(self, data: Dict[str, pd.DataFrame]) -> Dict[str, Dict[str, Any]]:
        """Generate summary information for each fetched dataset"""
        summary = {}
        
        for name, df in data.items():
            summary[name] = {
                'n_stations': len(df.columns),
                'n_rows': len(df),
                'start_date': df.index.min().strftime('%Y-%m-%d'),
                'end_date': df.index.max().strftime('%Y-%m-%d'),
                'data_type': 'Gridded',
                'dataset': name,
                'missing_percentage': (df.isna().sum().sum() / (df.shape[0] * df.shape[1])) * 100
            }
            
        return summary