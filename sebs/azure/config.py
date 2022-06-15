# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""Configuration management for Azure serverless benchmarking.

This module provides configuration classes for Azure resources, credentials,
and deployment settings. It handles Azure-specific configuration including
service principal authentication, resource group management, storage accounts,
and CosmosDB setup.

Key classes:
    AzureCredentials: Manages Azure service principal authentication
    AzureResources: Manages Azure resource allocation and lifecycle
    AzureConfig: Combines credentials and resources for Azure deployment
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import cast, Dict, List, Optional

from sebs.azure.cli import AzureCLI
from sebs.azure.cloud_resources import CosmosDBAccount
from sebs.cache import Cache
from sebs.faas.config import Config, Credentials, Resources
from sebs.utils import LoggingHandlers


class AzureCredentials(Credentials):
    """Azure service principal credentials for authentication.

    This class manages Azure service principal credentials required for
    authenticating with Azure services. It handles app ID, tenant ID,
    password, and subscription ID validation and caching.

    Attributes:
        _appId: Azure application (client) ID
        _tenant: Azure tenant (directory) ID
        _password: Azure client secret
        _subscription_id: Azure subscription ID (optional)
    """

    _appId: str
    _tenant: str
    _password: str
    _subscription_id: Optional[str]

    def __init__(
        self, appId: str, tenant: str, password: str, subscription_id: Optional[str] = None
    ) -> None:
        """Initialize Azure credentials.

        Args:
            appId: Azure application (client) ID
            tenant: Azure tenant (directory) ID
            password: Azure client secret
            subscription_id: Azure subscription ID (optional)
        """
        super().__init__()
        self._appId = appId
        self._tenant = tenant
        self._password = password
        self._subscription_id = subscription_id

    @property
    def appId(self) -> str:
        """Get the Azure application (client) ID.

        Returns:
            Azure application ID string.
        """
        return self._appId

    @property
    def tenant(self) -> str:
        """Get the Azure tenant (directory) ID.

        Returns:
            Azure tenant ID string.
        """
        return self._tenant

    @property
    def password(self) -> str:
        """Get the Azure client secret.

        Returns:
            Azure client secret string.
        """
        return self._password

    @property
    def subscription_id(self) -> str:
        """Get the Azure subscription ID.

        Returns:
            Azure subscription ID string.

        Raises:
            AssertionError: If subscription ID is not set.
        """
        assert self._subscription_id is not None
        return self._subscription_id

    @subscription_id.setter
    def subscription_id(self, subscription_id: str) -> None:
        """Set the Azure subscription ID with validation.

        Args:
            subscription_id: Azure subscription ID to set

        Raises:
            RuntimeError: If provided subscription ID conflicts with cached value.
        """
        if self._subscription_id is not None and subscription_id != self._subscription_id:
            self.logging.error(
                f"The subscription id {subscription_id} from provided "
                f"credentials is different from the subscription id "
                f"{self._subscription_id} found in the cache! "
                "Please change your cache directory or create a new one!"
            )
            raise RuntimeError(
                f"Azure login credentials do not match the subscription "
                f"{self._subscription_id} in cache!"
            )

        self._subscription_id = subscription_id

    @property
    def has_subscription_id(self) -> bool:
        """Check if subscription ID is set.

        Returns:
            True if subscription ID is set, False otherwise.
        """
        return self._subscription_id is not None

    @staticmethod
    def initialize(dct: dict, subscription_id: Optional[str]) -> "AzureCredentials":
        """Initialize credentials from dictionary.

        Args:
            dct: Dictionary containing credential information
            subscription_id: Optional subscription ID to set

        Returns:
            New AzureCredentials instance.
        """
        return AzureCredentials(dct["appId"], dct["tenant"], dct["password"], subscription_id)

    @staticmethod
    def deserialize(config: dict, cache: Cache, handlers: LoggingHandlers) -> Credentials:
        """Deserialize credentials from config and cache.

        Loads Azure credentials from either the configuration dictionary
        or environment variables, with subscription ID retrieved from cache.

        Args:
            config: Configuration dictionary
            cache: Cache instance for storing/retrieving cached values
            handlers: Logging handlers for error reporting

        Returns:
            AzureCredentials instance with loaded configuration.

        Raises:
            RuntimeError: If no valid credentials are found in config or environment.
        """
        cached_config = cache.get_config("azure")
        ret: AzureCredentials
        old_subscription_id: Optional[str] = None
        # Load cached values
        if cached_config and "credentials" in cached_config:
            if "subscription_id" in cached_config["credentials"]:
                old_subscription_id = cached_config["credentials"]["subscription_id"]

        # Check for new config
        if "credentials" in config and "appId" in config["credentials"]:
            ret = AzureCredentials.initialize(config["credentials"], old_subscription_id)
        elif "AZURE_SECRET_APPLICATION_ID" in os.environ:
            ret = AzureCredentials(
                os.environ["AZURE_SECRET_APPLICATION_ID"],
                os.environ["AZURE_SECRET_TENANT"],
                os.environ["AZURE_SECRET_PASSWORD"],
                old_subscription_id,
            )
        else:
            raise RuntimeError(
                "Azure login credentials are missing! Please set "
                "up environmental variables AZURE_SECRET_APPLICATION_ID and "
                "AZURE_SECRET_TENANT and AZURE_SECRET_PASSWORD"
            )
        ret.logging_handlers = handlers

        return ret

    def serialize(self) -> dict:
        """Serialize credentials to dictionary.

        We store only subscription ID to avoid unsecure storage of sensitive data.

        Returns:
            Dictionary containing serialized credential data.
        """
        if self._subscription_id is not None:
            out = {"subscription_id": self.subscription_id}
        else:
            out = {}
        return out

    def update_cache(self, cache_client: Cache) -> None:
        """Update credentials in cache.

        Args:
            cache_client: Cache instance to update
        """
        cache_client.update_config(val=self.serialize(), keys=["azure", "credentials"])


class AzureResources(Resources):
    """Azure resource management for SeBS benchmarking.

    This class manages Azure cloud resources including storage accounts,
    resource groups, and CosmosDB accounts.

    Attributes:
        _resource_group: Name of the Azure resource group
        _storage_accounts: List of storage accounts for function code
        _data_storage_account: Storage account for benchmark data
        _cosmosdb_account: CosmosDB account for NoSQL storage
    """

    class Storage:
        """Azure Storage Account wrapper.

        Represents an Azure Storage Account with connection details
        for use in serverless function deployment and data storage.

        Attributes:
            account_name: Name of the Azure storage account
            connection_string: Connection string for accessing the storage account
        """

        def __init__(self, account_name: str, connection_string: str) -> None:
            """Initialize Azure Storage account.

            Args:
                account_name: Name of the Azure storage account
                connection_string: Connection string for storage access
            """
            super().__init__()
            self.account_name = account_name
            self.connection_string = connection_string

        @staticmethod
        def from_cache(account_name: str, connection_string: str) -> "AzureResources.Storage":
            """Create Storage instance from cached data.

            Args:
                account_name: Name of the storage account
                connection_string: Connection string for the account

            Returns:
                New Storage instance with the provided details.

            Raises:
                AssertionError: If connection string is empty.
            """
            connection_string = AzureResources.Storage.query_connection_string(
                account_name, cli_instance
            )
            ret = AzureResources.Storage(account_name, connection_string)
            return ret

        @staticmethod
        def query_connection_string(account_name: str, cli_instance: AzureCLI) -> str:
            """Query connection string for storage account from Azure.

            Args:
                account_name: Name of the storage account
                cli_instance: Azure CLI instance for executing queries

            Returns:
                Connection string for the storage account.
            """
            ret = cli_instance.execute(
                "az storage account show-connection-string --name {}".format(account_name)
            )
            ret_dct = json.loads(ret.decode("utf-8"))
            connection_string = ret_dct["connectionString"]
            return connection_string

        def serialize(self) -> dict:
            """Serialize storage account to dictionary.

            Returns:
                Dictionary containing storage account information.
            """
            return vars(self)

        @staticmethod
        def deserialize(obj: dict) -> "AzureResources.Storage":
            """Deserialize storage account from dictionary.

            Args:
                obj: Dictionary containing storage account data

            Returns:
                New Storage instance from dictionary data.
            """
        self.logging.info("Starting allocation of storage account {}.".format(account_name))
        cli_instance.execute(
            (
                "az storage account create --name {0} --location {1} "
                "--resource-group {2} --sku {3}"
            ).format(
                account_name,
                self._region,
                self.resource_group(cli_instance),
                sku,
            )
        )
        self.logging.info("Storage account {} created.".format(account_name))
        return AzureResources.Storage.from_allocation(account_name, cli_instance)

    def update_cache(self, cache_client: Cache) -> None:
        """Update resource configuration in cache.

        Persists current resource state including storage accounts,
        data storage accounts, and resource groups to filesystem cache.

        Args:
            cache_client: Cache instance for storing configuration
        """
        super().update_cache(cache_client)
        cache_client.update_config(val=self.serialize(), keys=["azure", "resources"])

    @staticmethod
    def initialize(res: Resources, dct: dict) -> None:
        """Initialize resources from dictionary data.

        Populates resource instance with data from configuration dictionary.

        Args:
            res: Resources instance to initialize
            dct: Dictionary containing resource configuration
        """
        ret = cast(AzureResources, res)
        super(AzureResources, AzureResources).initialize(ret, dct)

        ret._resource_group = dct["resource_group"]
        if "storage_accounts" in dct:
            ret._storage_accounts = [
                AzureResources.Storage.deserialize(x) for x in dct["storage_accounts"]
            ]
        else:
            ret._storage_accounts = []

        if "data_storage_account" in dct:
            ret._data_storage_account = AzureResources.Storage.deserialize(
                dct["data_storage_account"]
            )

        if "cosmosdb_account" in dct:
            ret._cosmosdb_account = CosmosDBAccount.deserialize(dct["cosmosdb_account"])

    def serialize(self) -> dict:
        """Serialize resources to dictionary.

        Returns:
            Dictionary containing all resource configuration data.
        """
        out = super().serialize()
        out["storage_accounts"] = [x.serialize() for x in self._storage_accounts]
        if self._resource_group:
            out["resource_group"] = self._resource_group
        if self._cosmosdb_account:
            out["cosmosdb_account"] = self._cosmosdb_account.serialize()
        if self._data_storage_account:
            out["data_storage_account"] = self._data_storage_account.serialize()
        return out

    @staticmethod
    def deserialize(config: dict, cache: Cache, handlers: LoggingHandlers) -> Resources:
        """Deserialize resources from config and cache.

        Loads Azure resources from cache if available, otherwise from configuration.
        If no data is present, then we initialize an empty resources object.

        Args:
            config: Configuration dictionary
            cache: Cache instance for retrieving cached values
            handlers: Logging handlers for error reporting

        Returns:
            AzureResources instance with loaded configuration.
        """
        cached_config = cache.get_config("azure")
        ret = AzureResources()
        # Load cached values
        if cached_config and "resources" in cached_config and len(cached_config["resources"]) > 0:
            logging.info("Using cached resources for Azure")
            AzureResources.initialize(ret, cached_config["resources"])
        else:
            # Check for new config
            if "resources" in config:
                AzureResources.initialize(ret, config["resources"])
                ret.logging_handlers = handlers
                ret.logging.info("No cached resources for Azure found, using user configuration.")
            else:
                ret = AzureResources()
                ret.logging_handlers = handlers
                ret.logging.info("No resources for Azure found, initialize!")
        return ret


class AzureConfig(Config):
    """Complete Azure configuration for SeBS benchmarking.

    Combines Azure credentials and resources into a single configuration
    object for managing Azure serverless function deployments.

    Attributes:
        _credentials: Azure service principal credentials
        _resources: Azure resource management instance
    """

    def __init__(self, credentials: AzureCredentials, resources: AzureResources) -> None:
        """Initialize Azure configuration.

        Args:
            credentials: Azure service principal credentials
            resources: Azure resource management instance
        """
        super().__init__(name="azure")
        self._credentials = credentials
        self._resources = resources
        self._redis_host = redis_host
        self._redis_password = redis_password

    @property
    def credentials(self) -> AzureCredentials:
        """Get Azure credentials.

        Returns:
            AzureCredentials instance for authentication.
        """
        return self._credentials

    @property
    def resources(self) -> AzureResources:
        """Get Azure resources manager.

        Returns:
            AzureResources instance for resource management.
        """
        return self._resources

    @staticmethod
    def initialize(cfg: Config, dct: dict) -> None:
        """Initialize configuration from dictionary data.

        Args:
            cfg: Config instance to initialize
            dct: Dictionary containing configuration data
        """
        config = cast(AzureConfig, cfg)
        config._region = dct["region"]

    @staticmethod
    def deserialize(config: dict, cache: Cache, handlers: LoggingHandlers) -> Config:
        """Deserialize complete Azure configuration.

        Creates AzureConfig instance from configuration dictionary and cache,
        combining credentials and resources with region information.

        Args:
            config: Configuration dictionary
            cache: Cache instance for storing/retrieving cached values
            handlers: Logging handlers for error reporting

        Returns:
            AzureConfig instance with complete Azure configuration.
        """
        cached_config = cache.get_config("azure")
        credentials = cast(AzureCredentials, AzureCredentials.deserialize(config, cache, handlers))
        resources = cast(AzureResources, AzureResources.deserialize(config, cache, handlers))
        config_obj = AzureConfig(credentials, resources, cached_config["redis_host"], cached_config["redis_password"])
        config_obj.logging_handlers = handlers
        # Load cached values
        if cached_config:
            config_obj.logging.info("Using cached config for Azure")
            AzureConfig.initialize(config_obj, cached_config)
        else:
            config_obj.logging.info("Using user-provided config for Azure")
            AzureConfig.initialize(config_obj, config)

        resources.set_region(config_obj.region)
        return config_obj

    def update_cache(self, cache: Cache) -> None:
        """Update complete configuration in cache.

        Persists region, credentials, and resources to filesystem cache.

        Args:
            cache: Cache instance for storing configuration
        """
        cache.update_config(val=self.region, keys=["azure", "region"])
        self.credentials.update_cache(cache)
        self.resources.update_cache(cache)

    def serialize(self) -> dict:
        """Serialize complete configuration to dictionary.

        Returns:
            Dictionary containing all Azure configuration data.
        """
        out = {
            "name": "azure",
            "region": self._region,
            "credentials": self._credentials.serialize(),
            "resources": self._resources.serialize(),
            "redis_host": self._redis_host,
            "redis_password": self._redis_password,
        }
        return out
